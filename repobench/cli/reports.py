"""Report assembly for the CLI (PRD §104-112).

Everything a report needs, built from storage + on-disk task packages:
aggregate metrics, paired comparisons, Pareto frontier, segments, generation
stats and warnings — plus the trial-level rows behind `--format jsonl|csv`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from repobench.analysis.metrics import aggregate_trials, segment_breakdown
from repobench.analysis.pareto import pareto_frontier
from repobench.analysis.recommendation import recommend
from repobench.analysis.stats import paired_bootstrap
from repobench.benchmark.health import HealthReport
from repobench.benchmark.manifest import load_manifest
from repobench.config import RepoBenchConfig
from repobench.core.errors import RepoBenchError, UsageError
from repobench.core.paths import ProjectPaths
from repobench.core.types import TaskMetadata, TrialResult
from repobench.repository.git import GitRepo
from repobench.reporting.models import (
    InstructionGenerationStats,
    PairComparison,
    ReportData,
)
from repobench.storage.db import Storage

# Segment dimensions rendered in every report (PRD §109); instruction tier is
# the most decision-relevant slice — D tasks are solution-derived by construction.
SEGMENT_DIMENSIONS = ("task_type", "subsystem", "complexity", "instruction_confidence")


def load_task_metadata(
    paths: ProjectPaths, storage: Storage, task_ids: list[str]
) -> dict[str, TaskMetadata]:
    """Task metadata from on-disk packages, falling back to the tasks table."""
    result: dict[str, TaskMetadata] = {}
    for task_id in task_ids:
        meta_file = paths.task_dir(task_id) / "metadata.json"
        if meta_file.is_file():
            try:
                result[task_id] = TaskMetadata.model_validate_json(meta_file.read_text())
                continue
            except Exception:
                pass
        row = storage.get_task(task_id)
        if row:
            try:
                result[task_id] = TaskMetadata.model_validate(row)
            except Exception:
                continue
    return result


def _best_quality_target(metrics: dict) -> str | None:
    if not metrics:
        return None
    return min(
        metrics,
        key=lambda tid: (
            -metrics[tid].solve_rate,
            metrics[tid].time_p50_ms
            if metrics[tid].time_p50_ms is not None
            else math.inf,
            tid,
        ),
    )


def _generation_stats(tasks: dict[str, TaskMetadata]) -> InstructionGenerationStats | None:
    """Tier-D generation outcome (issue #12): the generation/generation_failed
    extras written at build time are surfaced here instead of going unread."""
    generated = sum(1 for task in tasks.values() if task.model_extra.get("generation"))
    failed = sum(1 for task in tasks.values() if task.model_extra.get("generation_failed"))
    if generated or failed:
        return InstructionGenerationStats(generated=generated, failed=failed)
    return None


def _harness_version_warnings(trials: list[TrialResult]) -> list[str]:
    """PRD §30: results from different harness versions are never silently mixed."""
    versions_by_target: dict[str, set[str]] = {}
    for trial in trials:
        if trial.harness_version:
            versions_by_target.setdefault(trial.target_id, set()).add(trial.harness_version)
    return [
        f"target {target_id} ran with multiple harness versions "
        f"({', '.join(sorted(versions))}) — its aggregated results mix configurations"
        for target_id, versions in sorted(versions_by_target.items())
        if len(versions) > 1
    ]


def _resolve_run(storage: Storage, run_id: str | None) -> dict:
    """The run row for an explicit id, or the newest run (PRD §111)."""
    if run_id:
        run_row = storage.get_run(run_id)
        if run_row is None:
            raise UsageError(f"unknown run: {run_id}")
        return run_row
    runs = storage.list_runs()
    if not runs:
        raise UsageError("no runs recorded — run `repobench run` first")
    return runs[0]


def build_report_data(
    root: Path, cfg: RepoBenchConfig, storage: Storage, *, run_id: str | None = None
) -> ReportData:
    """Assemble ReportData for a run (PRD §104-112)."""
    run_row = _resolve_run(storage, run_id)

    run_id_eff = run_row["run_id"]
    benchmark_id = run_row.get("benchmark_id")
    trials = storage.list_trials(run_id_eff)

    benchmark = storage.get_benchmark(benchmark_id) if benchmark_id else None
    benchmark_id = benchmark["benchmark_id"] if benchmark else benchmark_id
    task_ids = storage.benchmark_task_ids(benchmark_id) if benchmark_id else []
    paths = ProjectPaths(root)
    tasks = load_task_metadata(paths, storage, task_ids)

    metrics = aggregate_trials(trials)

    run_config: dict = {}
    if run_row.get("config_json"):
        try:
            run_config = json.loads(run_row["config_json"])
        except Exception:
            run_config = {}
    bootstrap_seed = run_config.get("bootstrap_seed", cfg.analysis.bootstrap_seed)

    comparisons: list[PairComparison] = []
    comparison_maps: dict[tuple[str, str], dict] = {}
    best_id = _best_quality_target(metrics)
    if best_id is not None:
        trials_best = [t for t in trials if t.target_id == best_id]
        for other in sorted(metrics):
            if other == best_id:
                continue
            trials_other = [t for t in trials if t.target_id == other]
            boot = paired_bootstrap(trials_best, trials_other, seed=bootstrap_seed)
            comparison_maps[(best_id, other)] = boot
            comparisons.append(
                PairComparison(
                    target_a=best_id,
                    target_b=other,
                    diff_pp=boot["mean_diff_pp"],
                    ci_lo_pp=boot["ci_lo_pp"],
                    ci_hi_pp=boot["ci_hi_pp"],
                    conclusive=boot["conclusive"],
                )
            )

    segments = {
        dimension: segment_breakdown(trials, tasks, dimension)
        for dimension in SEGMENT_DIMENSIONS
    }

    health: HealthReport | None = None
    if benchmark and benchmark.get("health_json"):
        try:
            health = HealthReport.model_validate_json(benchmark["health_json"])
        except Exception:
            health = None

    warnings = list(health.warnings) if health else []
    if not any("network" in warning.lower() for warning in warnings):
        warnings.append("No network isolation")
    warnings.extend(_harness_version_warnings(trials))

    # Single source of truth (PRD §89): the immutable manifest records the
    # repository the benchmark was built from. GitRepo is only a fallback for
    # runs whose benchmark row has no loadable manifest.
    repository = None
    manifest_path = benchmark.get("manifest_path") if benchmark else None
    if manifest_path:
        try:
            repository = load_manifest(Path(manifest_path)).repository
        except Exception:
            repository = None
    if repository is None and (root / ".git").exists():
        try:
            repository = GitRepo(root).remote_slug
        except RepoBenchError:
            repository = None

    return ReportData(
        benchmark_id=benchmark_id,
        repository=repository,
        run_id=run_id_eff,
        tasks_total=len(task_ids),
        health=health,
        targets=[metrics[tid] for tid in sorted(metrics)],
        comparisons=comparisons,
        recommendation=recommend(metrics, comparison_maps) if metrics else None,
        pareto=pareto_frontier(list(metrics.values())) if metrics else None,
        segments=segments,
        instruction_generation=_generation_stats(tasks),
        warnings=warnings,
        concurrency=run_config.get("jobs"),
        bootstrap_seed=bootstrap_seed,
    )


def load_trial_export(
    root: Path, storage: Storage, *, run_id: str | None = None
) -> tuple[list[TrialResult], dict[str, TaskMetadata]]:
    """Trial rows + joined task metadata for `report --format jsonl|csv` (issue #5)."""
    run_row = _resolve_run(storage, run_id)
    trials = storage.list_trials(run_row["run_id"])
    task_ids = sorted({t.task_id for t in trials})
    tasks = load_task_metadata(ProjectPaths(root), storage, task_ids)
    return trials, tasks
