"""Trial executor: the execution pipeline (PRD §32, §41-43, §59-64).

For each Task x ExecutionTarget the executor runs the 13-step pipeline:

    1. CREATE WORKSPACE      8. CAPTURE PATCH
    2. INSTALL (optional)    9. APPLY HIDDEN VERIFIER
    3. SANITIZE ENVIRONMENT 10. RUN TASK VERIFIER
    4. BUILD COMMAND        11. RUN REGRESSION VERIFIER
    5. START HARNESS        12. COLLECT METRICS
    6. PARSE USAGE          13. DESTROY WORKSPACE
    7. HANDLE TIMEOUT

Correctness is defined exclusively by the hidden verifiers — the harness exit
code is recorded but never defines the outcome (PRD §42, §63). execute() never
raises: unexpected failures surface as SETUP_ERROR/VERIFIER_ERROR results.

Install and verifier commands run in `project.cwd` when configured (issue #34,
monorepos); the harness itself always runs at the workspace root so the agent
sees the whole repo. A workspace missing project.cwd is a config problem
surfaced as SETUP_ERROR/VERIFIER_ERROR with a clear message — never a crash.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from repobench.config import ExecutionConfig, PricingRule, ProjectConfig, compose_cwd
from repobench.core.ids import new_trial_id
from repobench.core.types import (
    CommandSpec,
    ExecutionTarget,
    TaskPackage,
    TrialOutcome,
    TrialResult,
    utcnow,
)
from repobench.execution import pricing_catalog
from repobench.execution.adapters.base import HarnessAdapter
from repobench.execution.adapters.registry import get_adapter
from repobench.execution.environment import TrialEnvironment
from repobench.execution.process import MAX_OUTPUT_BYTES, run_process
from repobench.execution.usage import resolve_cost
from repobench.execution.workspace import (
    Workspace,
    WorkspaceManager,
    apply_git_patch,
    capture_agent_patch,
    snapshot_tree,
    verify_synthetic_invariants,
)

_LOG = logging.getLogger("repobench.execution.runner")

_INSTALL_TIMEOUT_SECONDS = 1800
_VERIFIER_TIMEOUT_SECONDS = 1800
_ERROR_TAIL_CHARS = 1500

# Harness version probed once per process (PRD §29): the same adapter would
# otherwise re-run `--version` for every trial of the same run.
_HARNESS_VERSION_CACHE: dict[str, str | None] = {}


def cached_harness_version(adapter: HarnessAdapter) -> str | None:
    key = adapter.name
    if key not in _HARNESS_VERSION_CACHE:
        try:
            _HARNESS_VERSION_CACHE[key] = adapter.version()
        except Exception as exc:
            _LOG.warning("harness %s: version probe failed: %s", key, exc)
            _HARNESS_VERSION_CACHE[key] = None
    return _HARNESS_VERSION_CACHE[key]


def harness_version_snapshot() -> dict[str, str | None]:
    """Copy of the per-process version cache — used by run manifests (PRD §30)."""
    return dict(_HARNESS_VERSION_CACHE)


def _write_output_artifact(path: Path, text: str) -> None:
    """Best-effort capped write (PRD §121): output artifacts must never break a trial.

    The cap mirrors process.py's capture cap (MAX_OUTPUT_BYTES, tail kept) so a
    future raise of one constant covers capture and artifacts together.
    """
    try:
        data = text.encode("utf-8", errors="replace")
        if len(data) > MAX_OUTPUT_BYTES:
            data = data[-MAX_OUTPUT_BYTES:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        _LOG.warning("output artifact write failed (%s): %s", path, exc)


def build_task_prompt(instruction: str, workspace: Path) -> str:
    """Focused natural-language prompt for the harness (PRD §97)."""
    return (
        f"You are working in the repository located at:\n{workspace}\n\n"
        "Instruction:\n"
        f"{instruction}\n\n"
        "Constraints:\n"
        "- Work only within this repository.\n"
        "- Make the minimal correct change.\n"
        "- You may read code and run the repository's existing tests.\n"
        "- Do not push or open pull requests.\n"
    )


class TrialExecutor:
    """Runs single trials end-to-end and never raises (PRD §99)."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        execution_cfg: ExecutionConfig,
        project_cfg: ProjectConfig,
        pricing: dict[str, PricingRule] | None = None,
        artifacts_dir: Path,
        on_result: Callable[[TrialResult], None] | None = None,
        adapter_lookup: Callable[[str], HarnessAdapter] | None = None,
    ) -> None:
        self.workspaces = workspaces
        self.execution_cfg = execution_cfg
        self.project_cfg = project_cfg
        self.pricing = pricing
        self.artifacts_dir = Path(artifacts_dir)
        self.on_result = on_result
        self._adapter_lookup = adapter_lookup or get_adapter

    # ------------------------------------------------------------------ API

    async def execute(
        self,
        task: TaskPackage,
        target: ExecutionTarget,
        *,
        run_id: str | None = None,
        benchmark_id: str | None = None,
        rollout: int = 1,
    ) -> TrialResult:
        started_at = utcnow()
        trial_id = new_trial_id()
        ctx: dict = {"phase": "setup", "ws": None, "artifacts_dir": self.artifacts_dir}
        try:
            return await self._execute(
                task,
                target,
                trial_id=trial_id,
                started_at=started_at,
                run_id=run_id,
                benchmark_id=benchmark_id,
                rollout=rollout,
                ctx=ctx,
            )
        except Exception as exc:  # never raise — a crashing trial is still a result (PRD §99)
            outcome = (
                TrialOutcome.VERIFIER_ERROR if ctx["phase"] == "verify" else TrialOutcome.SETUP_ERROR
            )
            result = TrialResult(
                trial_id=trial_id,
                run_id=run_id,
                benchmark_id=benchmark_id,
                task_id=task.task_id,
                target_id=target.id,
                rollout=rollout,
                harness=target.harness,
                model=target.model,
                provider=target.provider,
                outcome=outcome,
                started_at=started_at,
                error=f"unexpected error: {exc}",
            )
            self._publish(result, ctx)
            return result

    # ------------------------------------------------------------- pipeline

    async def _execute(
        self,
        task: TaskPackage,
        target: ExecutionTarget,
        *,
        trial_id: str,
        started_at: datetime,
        run_id: str | None,
        benchmark_id: str | None,
        rollout: int,
        ctx: dict,
    ) -> TrialResult:
        adapter = self._adapter_lookup(target.harness)
        base = {
            "trial_id": trial_id,
            "run_id": run_id,
            "benchmark_id": benchmark_id,
            "task_id": task.task_id,
            "target_id": target.id,
            "rollout": rollout,
            "harness": adapter.name,
            "harness_version": cached_harness_version(adapter),
            "model": target.model,
            "provider": target.provider,
            "started_at": started_at,
        }

        # 1. CREATE WORKSPACE + MATERIALIZE BASE + SYNTHETIC GIT INIT
        try:
            ws = self.workspaces.create(trial_id, task.task_id, task.base_tar)
        except Exception as exc:
            shutil.rmtree(self.workspaces.workspaces_dir / trial_id, ignore_errors=True)
            return self._finish(
                TrialResult(
                    **base,
                    outcome=TrialOutcome.SETUP_ERROR,
                    error=f"workspace setup failed: {exc}",
                ),
                ctx,
            )
        ctx["ws"] = ws

        # 1b. SYNTHETIC INVARIANTS (PRD §35): the materialized workspace must hold
        # exactly the synthetic base commit, with no remotes and no original branches.
        violations = verify_synthetic_invariants(ws.repo_dir)
        if violations:
            shutil.rmtree(self.workspaces.workspaces_dir / trial_id, ignore_errors=True)
            ctx["ws"] = None
            return self._finish(
                TrialResult(
                    **base,
                    outcome=TrialOutcome.SETUP_ERROR,
                    error="synthetic invariants violated: " + "; ".join(violations),
                ),
                ctx,
            )
        artifacts_dir = Path(ctx["artifacts_dir"])

        # 2. PREPARE ENVIRONMENT (optional project install)
        if self.project_cfg.install_command:
            install_error = await self._run_install(ws)
            if install_error is not None:
                return self._finish(
                    TrialResult(**base, outcome=TrialOutcome.SETUP_ERROR, error=install_error),
                    ctx,
                )

        # 3./4. SANITIZED ENVIRONMENT + COMMAND
        if target.timeout_minutes is not None:
            timeout_seconds = target.timeout_minutes * 60
        elif self.execution_cfg.timeout_seconds is not None:
            timeout_seconds = self.execution_cfg.timeout_seconds
        else:
            timeout_seconds = self.execution_cfg.timeout_minutes * 60
        prompt = build_task_prompt(task.instruction_text(), ws.repo_dir)
        # The final composed prompt is a first-class trial artifact, next to
        # agent.patch and trial.json — auditability for every adapter, including
        # the official ones whose prompt otherwise only ever exists in argv.
        prompt_path_str: str | None = None
        try:
            prompt_path = artifacts_dir / "trials" / trial_id / "prompt.md"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt)
            prompt_path_str = str(prompt_path)
        except OSError as exc:
            _LOG.warning("trial %s: prompt artifact write failed: %s", trial_id, exc)
        spec = adapter.build_command(
            target,
            prompt,
            ws.repo_dir,
            task_id=task.task_id,
            target_id=target.id,
            timeout_seconds=timeout_seconds,
        )

        # 5./6./7. START HARNESS, WAIT, TERMINATE CHILDREN (process group handled
        # by the process runner). The trial env must stay alive while the harness
        # runs, so the run happens inside the context manager.
        with TrialEnvironment(
            scrub_ssh_agent=self.execution_cfg.scrub_ssh_agent, extra_env=target.env
        ) as env:
            spec = spec.model_copy(update={"env": env})
            proc = await run_process(spec)

        if proc.spawn_error is not None:
            return self._finish(
                TrialResult(
                    **base,
                    outcome=TrialOutcome.SETUP_ERROR,
                    exit_code=proc.exit_code,
                    duration_ms=proc.duration_ms,
                    error=f"harness could not be started: {proc.spawn_error}",
                ),
                ctx,
            )

        usage = None
        try:
            usage = adapter.parse_output(proc.stdout, proc.stderr).usage
        except Exception as exc:
            # usage stays unknown, never invented (PRD §54) — but never silently
            _LOG.warning("trial %s: usage parse failed: %s", trial_id, exc)

        # 6b. OUTPUT ARTIFACTS (PRD §121): capped stdout/stderr live on disk next
        # to trial.json/prompt.md/agent.patch; the result records their paths.
        stdout_path = artifacts_dir / "trials" / trial_id / "stdout.log"
        stderr_path = artifacts_dir / "trials" / trial_id / "stderr.log"
        _write_output_artifact(stdout_path, proc.stdout)
        _write_output_artifact(stderr_path, proc.stderr)

        # 8. CAPTURE PATCH (working tree vs synthetic BASE, commits included)
        # plus the test-tamper classification of that same diff (issue #18).
        files_changed = loc_added = loc_removed = None
        agent_patch = None
        tampered_tests: list[str] = []
        patch_error: str | None = None
        patch_path = artifacts_dir / "trials" / trial_id / "agent.patch"
        try:
            stats = capture_agent_patch(ws.repo_dir, patch_path)
            files_changed, loc_added, loc_removed, tampered_tests = stats
            agent_patch = str(patch_path)
        except Exception as exc:
            _LOG.warning("trial %s: patch capture failed: %s", trial_id, exc)
            files_changed = loc_added = loc_removed = None
            agent_patch = None
            tampered_tests = []
            patch_error = f"patch capture failed: {exc}"

        task_verified: bool | None = None
        regression_verified: bool | None = None
        error: str | None = None

        if proc.timed_out:
            # 7. TIMEOUT: no verification; stats already recorded best-effort.
            outcome = TrialOutcome.TIMEOUT
        else:
            ctx["phase"] = "verify"
            outcome, task_verified, regression_verified, error = await self._verify(task, ws)

        # A patch-capture failure is never silent: it lands in the result's
        # error field alongside (but never masking) any verifier error.
        if patch_error is not None:
            error = f"{error}; {patch_error}" if error else patch_error

        # 12. COLLECT METRICS (cost attribution, PRD §55, issue #17):
        # harness-reported > user `pricing:` rule > bundled catalog estimate
        # (only when the model is known to the catalog) > unknown.
        pricing = self.pricing.get(target.model) if self.pricing else None
        cost_usd, cost_source = resolve_cost(
            usage, pricing, catalog_price=pricing_catalog.lookup(target.model)
        )

        result = TrialResult(
            **base,
            outcome=outcome,
            exit_code=proc.exit_code,
            timed_out=proc.timed_out,
            duration_ms=proc.duration_ms,
            usage=usage,
            task_verified=task_verified,
            regression_verified=regression_verified,
            cost_usd=cost_usd,
            cost_source=cost_source,
            files_changed=files_changed,
            loc_added=loc_added,
            loc_removed=loc_removed,
            agent_patch=agent_patch,
            tampered_tests=tampered_tests,
            prompt_path=prompt_path_str,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            workspace=str(ws.repo_dir),
            error=error,
        )
        # 13. DESTROY WORKSPACE + persist trial manifest + notify
        self._publish(result, ctx)
        return result

    # -------------------------------------------------------------- helpers

    async def _run_install(self, ws: Workspace) -> str | None:
        try:
            argv = shlex.split(self.project_cfg.install_command or "")
        except ValueError as exc:
            return f"invalid install_command: {exc}"
        # Issue #34: install runs in project.cwd when set; the harness below
        # still gets the repo root. cwd is validated relative at config load,
        # but a workspace may not contain the directory — a config problem,
        # reported with a clear message instead of a mystery spawn failure.
        run_dir = compose_cwd(ws.repo_dir, self.project_cfg)
        if not run_dir.is_dir():
            return (
                f"project.cwd {self.project_cfg.cwd!r} does not exist in the workspace "
                f"({ws.repo_dir}) — fix project.cwd in repobench.yml"
            )
        result = await run_process(
            CommandSpec(argv=argv, cwd=run_dir, timeout_seconds=_INSTALL_TIMEOUT_SECONDS)
        )
        if result.exit_code == 0:
            return None
        tail = (result.stderr or result.stdout or "").strip()[-_ERROR_TAIL_CHARS:]
        return f"install command failed (exit {result.exit_code}): {tail}"

    async def _verify(
        self, task: TaskPackage, ws: Workspace
    ) -> tuple[TrialOutcome, bool | None, bool | None, str | None]:
        """Steps 9-11: hidden verifier on a snapshot copy of the final tree (PRD §62)."""
        try:
            verify_ws = snapshot_tree(ws.repo_dir, ws.base_dir / "verify")
        except Exception as exc:
            return TrialOutcome.VERIFIER_ERROR, None, None, f"verification snapshot failed: {exc}"

        ok, err = apply_git_patch(verify_ws, task.verifier_patch)
        if not ok:
            tail = (err or "").strip()[-_ERROR_TAIL_CHARS:]
            return TrialOutcome.VERIFIER_ERROR, None, None, f"verifier patch failed to apply: {tail}"

        if not self.project_cfg.test_command:
            return (
                TrialOutcome.VERIFIER_ERROR,
                None,
                None,
                "no test_command configured (set project.test_command in repobench.yml)",
            )

        # Issue #34: verifiers run in project.cwd inside the snapshot copy; the
        # agent-visible workspace root is untouched. Missing there is a config
        # problem → VERIFIER_ERROR with a clear message, never a crash.
        verify_dir = compose_cwd(verify_ws, self.project_cfg)
        if not verify_dir.is_dir():
            return (
                TrialOutcome.VERIFIER_ERROR,
                None,
                None,
                f"project.cwd {self.project_cfg.cwd!r} does not exist in the verification "
                f"workspace ({verify_ws}) — fix project.cwd in repobench.yml",
            )

        task_verified = await self._run_verifier(self.project_cfg.test_command, verify_dir)
        if isinstance(task_verified, str):
            return TrialOutcome.VERIFIER_ERROR, None, None, task_verified
        if task_verified is False:
            # Hidden task verifier failed: the trial is UNSOLVED (PRD §63).
            return TrialOutcome.UNSOLVED, False, None, None

        regression_command = self.project_cfg.regression_command or self.project_cfg.test_command
        regression_verified = await self._run_verifier(regression_command, verify_dir)
        if isinstance(regression_verified, str):
            return TrialOutcome.VERIFIER_ERROR, True, None, regression_verified

        if task_verified and regression_verified:
            return TrialOutcome.SOLVED, True, True, None
        return TrialOutcome.UNSOLVED, task_verified, regression_verified, None

    async def _run_verifier(self, command: str, run_dir: Path) -> bool | str:
        """True=pass, False=fail (exit 1), str=verifier crashed (any other exit).
        run_dir is the composed project.cwd directory (issue #34), pre-checked
        for existence by the caller."""
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"invalid verifier command {command!r}: {exc}"
        result = await run_process(
            CommandSpec(argv=argv, cwd=run_dir, timeout_seconds=_VERIFIER_TIMEOUT_SECONDS)
        )
        if result.exit_code == 0:
            return True
        if result.exit_code == 1:
            return False
        tail = (result.stderr or result.stdout or "").strip()[-_ERROR_TAIL_CHARS:]
        return f"verifier crashed (exit {result.exit_code}): {tail}"

    def _publish(self, result: TrialResult, ctx: dict) -> None:
        """Destroy workspace, persist the trial manifest, notify (PRD §59, §99-100).

        Never raises — but a failure here is never silent either.
        """
        ws = ctx.get("ws")
        if ws is not None:
            try:
                self.workspaces.destroy(ws)
            except Exception as exc:
                _LOG.warning("trial %s: workspace destroy failed: %s", result.trial_id, exc)
            ctx["ws"] = None
        artifacts_dir = ctx.get("artifacts_dir") or self.artifacts_dir
        if artifacts_dir is not None:
            try:
                trial_dir = Path(artifacts_dir) / "trials" / result.trial_id
                trial_dir.mkdir(parents=True, exist_ok=True)
                (trial_dir / "trial.json").write_text(result.model_dump_json(indent=2))
            except Exception as exc:
                _LOG.warning("trial %s: trial manifest write failed: %s", result.trial_id, exc)
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:
                _LOG.warning("trial %s: on_result callback failed: %s", result.trial_id, exc)

    def _finish(self, result: TrialResult, ctx: dict) -> TrialResult:
        self._publish(result, ctx)
        return result


async def run_matrix(
    pairs: list[tuple[TaskPackage, ExecutionTarget, int]],
    executor: TrialExecutor,
    *,
    run_id: str | None = None,
    benchmark_id: str | None = None,
    jobs: int = 1,
    progress: Callable[[TrialResult], None] | None = None,
) -> list[TrialResult]:
    """Execute the given (task, target, rollout) pairs with bounded concurrency
    (PRD §57, issue #13). Rollout expansion happens in the planner; this stays a
    dumb executor. Results are returned in completion order.
    """
    results: list[TrialResult] = []
    semaphore = asyncio.Semaphore(max(1, jobs))

    async def _one(task: TaskPackage, target: ExecutionTarget, rollout: int) -> None:
        async with semaphore:
            trial = await executor.execute(
                task, target, run_id=run_id, benchmark_id=benchmark_id, rollout=rollout
            )
            results.append(trial)
            if progress is not None:
                try:
                    progress(trial)
                except Exception as exc:
                    _LOG.warning(
                        "progress callback failed for trial %s: %s", trial.trial_id, exc
                    )

    await asyncio.gather(*(_one(task, target, rollout) for task, target, rollout in pairs))
    return results
