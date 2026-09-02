"""Service layer behind the RepoBench CLI (PRD §90-112).

Commands stay thin: each one delegates to a function here, so tests (and the
e2e suite) can drive exactly the same code paths as the CLI. Persistence lives
at this layer only — the domain modules stay pure (PRD §114).

Scope: project layout/init, analyze, and run planning/execution. Benchmark
construction lives in cli.builds; report assembly in cli.reports; run
inspection and .repobench/ GC in cli.maintenance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from repobench.config import RepoBenchConfig, default_config_for
from repobench.core.errors import UsageError
from repobench.core.ids import new_run_id
from repobench.core.paths import ProjectPaths, find_repo_root
from repobench.core.types import (
    AnalyzeSummary,
    CandidateInfo,
    ExecutionTarget,
    PRInfo,
    TaskPackage,
    TaskStatus,
    TrialOutcome,
    TrialResult,
    utcnow,
)
from repobench.execution.adapters.registry import get_adapter
from repobench.execution.runner import TrialExecutor, run_matrix
from repobench.execution.workspace import WorkspaceManager
from repobench.mining.candidates import mine_candidates
from repobench.mining.subsystem import detect_package_dirs, load_codeowners
from repobench.repository.git import GitRepo
from repobench.repository.github import GitHubClient
from repobench.repository.workload import suggest_benchmark_size, summarize_analysis
from repobench.storage.db import Storage

CONFIG_FILENAME = "repobench.yml"

ProgressFn = Callable[[str], None]


# --------------------------------------------------------------------- layout


def resolve_repo_root(start: Path | None = None) -> Path:
    """Git work tree containing `start`, or a polite error (PRD §95)."""
    root = find_repo_root(start or Path.cwd())
    if root is None:
        raise UsageError(
            "not inside a git repository — cd into your project repository first"
        )
    return root


def project_paths(root: Path) -> ProjectPaths:
    """ProjectPaths for a repo root with the .repobench/ tree ensured (PRD §115)."""
    paths = ProjectPaths(root)
    paths.ensure()
    return paths


def config_path_for(root: Path, explicit: Path | None) -> Path:
    """Explicit --config path wins; otherwise repobench.yml at the repo root."""
    if explicit is not None:
        return explicit
    return root / CONFIG_FILENAME


def load_config(root: Path, explicit: Path | None = None) -> RepoBenchConfig:
    path = config_path_for(root, explicit)
    if explicit is None and not path.is_file():
        raise UsageError(f"{CONFIG_FILENAME} not found — run `repobench init` first")
    return RepoBenchConfig.load(path)


# ----------------------------------------------------------------------- init


@dataclass
class InitOutcome:
    root: Path
    config_path: Path
    created: bool
    gitignore_updated: bool
    config: RepoBenchConfig


def _ensure_gitignore(root: Path) -> bool:
    """Append `.repobench/` to .gitignore when missing (PRD §115)."""
    gitignore = root / ".gitignore"
    lines: list[str] = []
    if gitignore.is_file():
        lines = gitignore.read_text().splitlines()
    if any(line.strip() == ".repobench/" for line in lines):
        return False
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    text += ".repobench/\n"
    gitignore.write_text(text)
    return True


def init_project(root: Path, *, overwrite: bool) -> InitOutcome:
    """Detect the project environment and write repobench.yml (PRD §95)."""
    config_path = root / CONFIG_FILENAME
    created = not config_path.exists()
    if config_path.exists() and not overwrite:
        raise UsageError(
            f"{CONFIG_FILENAME} already exists — use --force to overwrite it"
        )
    cfg = default_config_for(root)
    project_paths(root)
    cfg.save(config_path)
    gitignore_updated = _ensure_gitignore(root)
    return InitOutcome(
        root=root,
        config_path=config_path,
        created=created,
        gitignore_updated=gitignore_updated,
        config=cfg,
    )


# --------------------------------------------------------------------- analyze


@dataclass
class AnalyzeOutcome:
    summary: AnalyzeSummary
    candidates: list[CandidateInfo]
    merged_prs: int
    enrichment: str  # "github" | "local"
    remote_slug: str | None
    # None = unknown (no gh / no remote); True triggers the PRD §51 warning.
    public_repository: bool | None = None


def _local_enricher(repo: GitRepo) -> Callable[[PRInfo], PRInfo]:
    """Offline enrichment: the first commit subject on the PR branch becomes the
    PR title, giving `derive_instruction` a (confidence C) instruction source.
    Without this, repositories without `gh` would yield zero usable candidates."""

    def enrich(pr: PRInfo) -> PRInfo:
        if not (pr.base_sha and pr.head_sha):
            return pr
        hint = repo.pr_title_hint(pr.base_sha, pr.head_sha)
        title = hint.splitlines()[0].strip() if hint else ""
        if not title:
            return pr
        return pr.model_copy(update={"title": title})

    return enrich


# Cached per process (analyze and benchmark build share one probe — PRD §51).
@lru_cache(maxsize=None)
def repository_visibility(slug: str | None) -> str | None:
    """PUBLIC | PRIVATE | None(unknown) via `gh repo view` — best-effort."""
    if not slug or not shutil.which("gh"):
        return None
    return GitHubClient(slug).visibility()


def analyze_repository(root: Path, cfg: RepoBenchConfig) -> AnalyzeOutcome:
    """Mine the Workload Universe and eval candidates (PRD §10, §65-66)."""
    repo = GitRepo(root)
    slug = repo.remote_slug
    enrich: Callable[[PRInfo], PRInfo] | None
    if slug and shutil.which("gh"):
        enrich = GitHubClient(slug).enrich
        enrichment = "github"
    else:
        enrich = _local_enricher(repo)
        enrichment = "local"
    merged = repo.merged_prs(cfg.repository.lookback_days)
    candidates = mine_candidates(
        repo,
        cfg.task_mining,
        enrich=enrich,
        lookback_days=cfg.repository.lookback_days,
        codeowners=load_codeowners(root),
        package_dirs=detect_package_dirs(root),
    )
    pool = [c for c in candidates if c.status is not TaskStatus.FILTERED]
    summary = summarize_analysis(
        len(merged), candidates, suggest_benchmark_size(len(pool))
    )
    return AnalyzeOutcome(
        summary=summary,
        candidates=candidates,
        merged_prs=len(merged),
        enrichment=enrichment,
        remote_slug=slug,
        public_repository=(repository_visibility(slug) == "PUBLIC") if slug else None,
    )


def persist_candidates(storage: Storage, candidates: list[CandidateInfo]) -> None:
    for candidate in candidates:
        storage.save_candidate(candidate)


# ------------------------------------------------------------------------ run


def resolve_targets(
    cfg: RepoBenchConfig, target_ids: list[str], all_targets: bool
) -> list[ExecutionTarget]:
    """CLI target selection: ids must exist in config.targets, or --all (PRD §93)."""
    configured = cfg.targets
    available = ", ".join(sorted(configured)) or "(none configured)"
    if all_targets:
        targets = list(configured.values())
        if not targets:
            raise UsageError(f"no targets configured in {CONFIG_FILENAME}")
        return targets
    if not target_ids:
        raise UsageError(
            f"no targets specified — pass target ids or --all (configured: {available})"
        )
    unknown = [t for t in target_ids if t not in configured]
    if unknown:
        raise UsageError(
            f"unknown target(s): {', '.join(unknown)} (configured: {available})"
        )
    seen: set[str] = set()
    ordered: list[ExecutionTarget] = []
    for target_id in target_ids:
        if target_id not in seen:
            seen.add(target_id)
            ordered.append(configured[target_id])
    return ordered


def validate_targets(targets: list[ExecutionTarget]) -> None:
    """Structural validation only — no inference (PRD §94)."""
    for target in targets:
        result = get_adapter(target.harness).validate_target(target)
        if not result.valid:
            raise UsageError(
                f"target {target.id!r} is invalid: " + "; ".join(result.errors)
            )


def ensure_custom_command_trust(
    storage: Storage,
    targets: list[ExecutionTarget],
    *,
    trusted: bool,
) -> None:
    """The PRD §26 gate for generic-command targets.

    A command target may only execute when the user passed
    --trust-custom-command, persisted execution.trust_custom_commands in
    repobench.yml, or the exact same command template already ran before
    (registered in execution_targets at first execution). Anything else raises
    UsageError listing every template for review.
    """
    untrusted: list[ExecutionTarget] = []
    for target in targets:
        if target.harness != "command" or trusted:
            continue
        registered = storage.get_target(target.id)
        same_template = registered is not None and registered.get("command") == (
            target.command or []
        )
        if not same_template:
            untrusted.append(target)
    if untrusted:
        lines = [
            "custom command targets are not trusted yet — review before first execution:",
            *(f"  {t.id}: {shlex.join(t.command or [])}" for t in untrusted),
            "re-run with --trust-custom-command or set "
            "execution.trust_custom_commands: true in repobench.yml (PRD §26)",
        ]
        raise UsageError("\n".join(lines))


def resolve_benchmark(storage: Storage, benchmark_id: str | None) -> dict:
    if benchmark_id:
        row = storage.get_benchmark(benchmark_id)
        if row is None:
            raise UsageError(f"unknown benchmark: {benchmark_id}")
        return row
    rows = storage.list_benchmarks()
    if not rows:
        raise UsageError(
            "no benchmark found — run `repobench benchmark build` first"
        )
    return rows[0]


def load_benchmark_packages(
    paths: ProjectPaths, storage: Storage, benchmark_id: str
) -> list[TaskPackage]:
    task_ids = storage.benchmark_task_ids(benchmark_id)
    if not task_ids:
        raise UsageError(f"benchmark {benchmark_id} has no tasks — rebuild it")
    packages: list[TaskPackage] = []
    for task_id in task_ids:
        directory = paths.task_dir(task_id)
        if not (directory / "metadata.json").is_file():
            raise UsageError(
                f"task package missing on disk: {directory} — rebuild the benchmark"
            )
        packages.append(TaskPackage.load(directory))
    return packages


@dataclass
class RunPlan:
    run_id: str
    benchmark_id: str
    tasks: list[TaskPackage]
    targets: list[ExecutionTarget]
    # (task, target, rollout) triples — rollout expansion happens here in the
    # planner, so run_matrix stays a dumb bounded-concurrency executor (issue #13).
    pairs: list[tuple[TaskPackage, ExecutionTarget, int]]
    jobs: int
    timeout_minutes: int
    keep_workspaces: bool
    is_resume: bool
    already_complete: int
    retried: int = 0
    rollouts: int = 1


# Outcomes a plain --resume retries: infrastructure/transient failures, never a
# verdict. TIMEOUT is retryable because it may reflect machine contention, not
# the target's ability (issue #10). --retry-failed additionally retries UNSOLVED.
_RETRYABLE_OUTCOMES = (
    TrialOutcome.SETUP_ERROR,
    TrialOutcome.VERIFIER_ERROR,
    TrialOutcome.HARNESS_ERROR,
    TrialOutcome.TIMEOUT,
)


def plan_run(
    storage: Storage,
    paths: ProjectPaths,
    cfg: RepoBenchConfig,
    *,
    targets: list[ExecutionTarget],
    benchmark_id: str | None = None,
    resume: bool = False,
    retry_failed: bool = False,
    jobs: int | None = None,
    keep: bool | None = None,
    rollouts: int = 1,
) -> RunPlan:
    """Resolve benchmark, tasks and the exact (Task, Target, rollout) triples to
    execute (PRD §96, §99; multi-rollout expansion per issue #13)."""
    if rollouts < 1:
        raise UsageError(f"--rollouts must be at least 1, got {rollouts}")
    if resume:
        runs = storage.list_runs()
        if not runs:
            raise UsageError("nothing to resume — no runs recorded yet")
        run_row = runs[0]
        run_id = run_row["run_id"]
        benchmark_id = run_row.get("benchmark_id")
        if not benchmark_id:
            raise UsageError(f"run {run_id} has no benchmark; cannot resume")
        is_resume = True
    else:
        row = resolve_benchmark(storage, benchmark_id)
        benchmark_id = row["benchmark_id"]
        run_id = new_run_id()
        is_resume = False

    tasks = load_benchmark_packages(paths, storage, benchmark_id)
    jobs_eff = jobs if jobs is not None else cfg.execution.jobs
    keep_eff = keep if keep is not None else cfg.execution.keep_workspaces

    existing: dict[tuple[str, str, int], list[TrialResult]] = {}
    if is_resume:
        for trial in storage.list_trials(run_id):
            existing.setdefault((trial.task_id, trial.target_id, trial.rollout), []).append(
                trial
            )

    retryable = set(_RETRYABLE_OUTCOMES)
    if retry_failed:
        retryable.add(TrialOutcome.UNSOLVED)
    pairs: list[tuple[TaskPackage, ExecutionTarget, int]] = []
    already_complete = 0
    retried = 0
    for task in tasks:
        for target in targets:
            for rollout in range(1, rollouts + 1):
                previous = existing.get((task.task_id, target.id, rollout), [])
                # A rollout is already complete iff a stored trial settled it
                # with a non-retryable verdict (issue #13).
                if any(trial.outcome not in retryable for trial in previous):
                    already_complete += 1
                    continue
                if previous:
                    retried += 1
                pairs.append((task, target, rollout))

    return RunPlan(
        run_id=run_id,
        benchmark_id=benchmark_id,
        tasks=tasks,
        targets=targets,
        pairs=pairs,
        jobs=max(1, jobs_eff),
        timeout_minutes=cfg.execution.timeout_minutes,
        keep_workspaces=keep_eff,
        is_resume=is_resume,
        already_complete=already_complete,
        retried=retried,
        rollouts=rollouts,
    )


@dataclass
class RunOutcome:
    plan: RunPlan
    results: list[TrialResult]


def _record_reproducibility(
    storage: Storage,
    paths: ProjectPaths,
    *,
    root: Path,
    plan: RunPlan,
    bootstrap_seed: int,
) -> None:
    """Persist the run's reproducibility record (PRD §29-31): register every
    target's fingerprint (also the persisted trust anchor for generic-command
    templates, PRD §26) and write runs/<id>/manifest.json. Best-effort — a
    manifest write failure is logged, never fatal to the run."""
    from repobench import __version__
    from repobench.execution.fingerprint import (
        build_run_manifest,
        instruction_file_hashes,
        target_fingerprint,
    )
    from repobench.execution.runner import cached_harness_version, harness_version_snapshot

    for target in plan.targets:
        fingerprint = target_fingerprint(target)
        storage.save_target(
            target.id,
            definition_json=json.dumps(fingerprint["definition"]),
            fingerprint_json=json.dumps(fingerprint),
        )
        # Probe harness versions now so the manifest (written before the matrix
        # starts) already carries them; trial time reuses the same cache.
        cached_harness_version(get_adapter(target.harness))

    manifest = build_run_manifest(
        run_id=plan.run_id,
        benchmark_id=plan.benchmark_id,
        targets=plan.targets,
        harness_versions=harness_version_snapshot(),
        instruction_hashes=instruction_file_hashes(root),
        bootstrap_seed=bootstrap_seed,
        started_at=utcnow().isoformat(),
        repobench_version=__version__,
    )
    manifest_path = paths.run_dir(plan.run_id) / "manifest.json"
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
    except OSError as exc:
        logging.getLogger("repobench.cli").warning("run manifest write failed: %s", exc)


def execute_plan(
    root: Path,
    cfg: RepoBenchConfig,
    storage: Storage,
    plan: RunPlan,
    *,
    progress: Callable[[int, int, TrialResult], None] | None = None,
) -> RunOutcome:
    """Execute the planned matrix, persisting each trial (PRD §32, §96-99)."""
    paths = project_paths(root)
    exec_cfg = cfg.execution.model_copy(
        update={"jobs": plan.jobs, "keep_workspaces": plan.keep_workspaces}
    )
    manager = WorkspaceManager(paths.workspaces_dir, keep=plan.keep_workspaces)
    executor = TrialExecutor(
        workspaces=manager,
        execution_cfg=exec_cfg,
        project_cfg=cfg.project,
        pricing=dict(cfg.pricing),
        artifacts_dir=paths.run_dir(plan.run_id),
        on_result=storage.save_trial,
    )

    bootstrap_seed = cfg.analysis.bootstrap_seed
    config_json = json.dumps(
        {
            "benchmark_id": plan.benchmark_id,
            "jobs": plan.jobs,
            "timeout_minutes": plan.timeout_minutes,
            "keep_workspaces": plan.keep_workspaces,
            "bootstrap_seed": bootstrap_seed,
            "targets": [t.id for t in plan.targets],
            "rollouts": plan.rollouts,
        }
    )
    if plan.is_resume:
        storage.execute(
            "UPDATE runs SET status = 'RUNNING', finished_at = NULL, config_json = ? "
            "WHERE run_id = ?",
            (config_json, plan.run_id),
        )
    else:
        storage.create_run(plan.run_id, plan.benchmark_id, config_json=config_json)

    _record_reproducibility(
        storage, paths, root=root, plan=plan, bootstrap_seed=bootstrap_seed
    )

    total = len(plan.pairs)
    done = 0

    def on_result(trial: TrialResult) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total, trial)

    results = asyncio.run(
        run_matrix(
            plan.pairs,
            executor,
            run_id=plan.run_id,
            benchmark_id=plan.benchmark_id,
            jobs=plan.jobs,
            progress=on_result,
        )
    )
    storage.finish_run(plan.run_id, status="COMPLETED")
    return RunOutcome(plan=plan, results=results)
