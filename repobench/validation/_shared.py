"""Shared plumbing for validation checks (PRD §77-81).

The five historical-validation checks are declarative CheckSpec rows executed by a
single runner (`_run_spec`) that owns the whole skeleton exactly once: materialize a
fresh workspace from the task's base archive via WorkspaceManager (unique uuid4
trial ids, workspaces dir under a caller-provided root, keep=False), optionally run
the install command, apply the gold/verifier patches directly in the disposable
workspace repo, then run one command and triage its exit code.

Exit-code semantics everywhere: 0 = pass, 1 = fail, anything else (including
timeouts/spawn failures) = inconclusive — a verifier infrastructure problem that
is surfaced in `details` with the tail of stdout/stderr. Inverted checks (noop,
PRD §78) swap the two: a decisive exit 1 means the check passed. Commands always
run with the plain inherited environment (env=None): trial-env sanitization is for
harness execution, not for verifier runs.
"""

from __future__ import annotations

import shlex
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pydantic

from repobench.config import ProjectConfig
from repobench.core.types import ProcessResult, RejectionCode, TaskPackage
from repobench.execution.process import run_sync
from repobench.execution.workspace import Workspace, WorkspaceManager, apply_git_patch

OUTPUT_TAIL_CHARS = 1500
GOLD_APPLY_FAILED = "gold patch failed to apply"


class CheckResult(pydantic.BaseModel):
    """Result of a single validation check (PRD §77-81)."""

    name: str
    passed: bool | None  # None = skipped
    code: RejectionCode | None = None
    details: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, kw_only=True)
class CheckSpec:
    """Declarative description of one historical-validation check (PRD §77-81).

    `pass_description`/`fail_description` must state what a decisive outcome means
    for this check's own command and workspace instead of being derived from the
    apply_gold/apply_verifier flags: checks sharing a flag combination run different
    commands over the same workspace state (oracle runs test_command over
    gold+verifier; regression runs regression_command over that same state).
    """

    name: str
    command_getter: Callable[[ProjectConfig], str | None]
    apply_gold: bool = False
    apply_verifier: bool = False
    invert: bool = False  # True for noop: decisive exit 1 means the check PASSED
    fail_code: RejectionCode  # code when the decisive exit is the failing one
    pass_description: str  # details for a decisive pass
    fail_description: str  # decisive-fail details; exit code and output tail are appended unless invert


def get_test_command(project: ProjectConfig) -> str | None:
    return project.test_command


def get_regression_command(project: ProjectConfig) -> str | None:
    return project.regression_command


def split_command(command: str | None) -> list[str] | None:
    """Parse a configured command string into an argv list (never shell=True)."""
    if command is None or not command.strip():
        return None
    return shlex.split(command)


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def output_tail(result: ProcessResult, limit: int = OUTPUT_TAIL_CHARS) -> str:
    """Last ~`limit` chars of captured stdout/stderr, for surfacing in details."""
    parts: list[str] = []
    out = result.stdout.strip()
    err = result.stderr.strip()
    if out:
        parts.append(f"stdout (tail): {out[-limit:]}")
    if err:
        parts.append(f"stderr (tail): {err[-limit:]}")
    if result.timed_out:
        parts.append("command timed out")
    return " | ".join(parts)


def new_trial(task: TaskPackage, workspaces_root: Path) -> tuple[WorkspaceManager, Workspace]:
    """Fresh workspace from task.base_tar under a unique validation directory."""
    token = uuid.uuid4().hex
    workspaces_dir = Path(workspaces_root) / f"validate_{token[:12]}"
    manager = WorkspaceManager(workspaces_dir=workspaces_dir, keep=False)
    ws = manager.create(trial_id=token, task_id=task.task_id, base_archive=task.base_tar)
    return manager, ws


def check_skipped(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, passed=None, details=f"skipped: {reason}")


def check_inconclusive(name: str, start: float, message: str) -> CheckResult:
    # code=None: inconclusive = verifier infrastructure problem, not a task defect.
    return CheckResult(
        name=name, passed=False, code=None, details=message, duration_ms=elapsed_ms(start)
    )


def _command_attr(spec: CheckSpec) -> str:
    """ProjectConfig attribute backing the spec's command, derived from the getter
    name (get_test_command -> test_command) so skip/inconclusive messages always
    name the attribute that was actually consulted."""
    return spec.command_getter.__name__.removeprefix("get_")


def _fail_details(spec: CheckSpec, result: ProcessResult) -> str:
    """Decisive-fail details: the spec's own description plus the exit code and the
    output tail — except for inverted checks, whose description already tells the
    whole story (the verifier passing on BASE needs no output)."""
    if spec.invert:
        return spec.fail_description
    return f"{spec.fail_description} (exit {result.exit_code}): {output_tail(result)}"


def _run_spec(
    task: TaskPackage, project: ProjectConfig, spec: CheckSpec, *, workspaces_root: Path
) -> CheckResult:
    """Execute one CheckSpec: the entire check skeleton, written exactly once."""
    argv = split_command(spec.command_getter(project))
    if argv is None:
        return check_skipped(spec.name, f"no {_command_attr(spec)} configured")
    start = time.monotonic()
    manager = ws = None
    try:
        manager, ws = new_trial(task, workspaces_root)
        install_argv = split_command(project.install_command)
        if install_argv is not None:
            install = run_sync(install_argv, ws.repo_dir)
            if install.exit_code != 0:
                return check_inconclusive(
                    spec.name,
                    start,
                    f"install command failed (exit {install.exit_code}): {output_tail(install)}",
                )
        if spec.apply_gold:
            applied, err = apply_git_patch(ws.repo_dir, task.gold_patch)
            if not applied:  # the gold solution itself is broken — a task defect
                return CheckResult(
                    name=spec.name,
                    passed=False,
                    code=spec.fail_code,
                    details=f"{GOLD_APPLY_FAILED}: {err.strip()[-OUTPUT_TAIL_CHARS:]}",
                    duration_ms=elapsed_ms(start),
                )
        if spec.apply_verifier:
            applied, err = apply_git_patch(ws.repo_dir, task.verifier_patch)
            if not applied:
                return check_inconclusive(
                    spec.name,
                    start,
                    f"verifier patch failed to apply: {err.strip()[-OUTPUT_TAIL_CHARS:]}",
                )
        result = run_sync(argv, ws.repo_dir)
        decisive_pass = (result.exit_code == 1) if spec.invert else (result.exit_code == 0)
        if decisive_pass:
            return CheckResult(
                name=spec.name,
                passed=True,
                details=spec.pass_description,
                duration_ms=elapsed_ms(start),
            )
        if result.exit_code in (0, 1):
            return CheckResult(
                name=spec.name,
                passed=False,
                code=spec.fail_code,
                details=_fail_details(spec, result),
                duration_ms=elapsed_ms(start),
            )
        # Any other exit (timeout, spawn failure, unexpected code) is inconclusive.
        noun = _command_attr(spec).replace("_", " ")
        return check_inconclusive(
            spec.name, start, f"{noun} exited {result.exit_code}: {output_tail(result)}"
        )
    finally:
        if manager is not None and ws is not None:
            manager.destroy(ws)
            shutil.rmtree(manager.workspaces_dir, ignore_errors=True)
