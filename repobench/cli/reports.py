"""Report assembly for the CLI (PRD §104-112).

Everything a report needs, built from storage + on-disk task packages:
aggregate metrics, paired comparisons, Pareto frontier, segments, generation
stats and warnings — plus the trial-level rows behind `--format jsonl|csv`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from repobench.analysis.metrics import aggregate_trials, segment_breakdown
from repobench.analysis.pareto import pareto_frontier
from repobench.analysis.recommendation import recommend
from repobench.analysis.reliability import TargetReliability, reliability_stats
from repobench.analysis.stats import paired_bootstrap
from repobench.benchmark.health import HealthReport
from repobench.benchmark.manifest import load_stored_manifest
from repobench.config import RepoBenchConfig
from repobench.core.errors import RepoBenchError, UsageError
from repobench.core.paths import ProjectPaths
from repobench.core.types import TaskMetadata, TrialResult
from repobench.execution import pricing_catalog
from repobench.repository.git import GitRepo
from repobench.reporting.models import (
    TAMPERED_PATHS_CAP,
    InstructionGenerationStats,
    PairComparison,
    ReportData,
    TestTamperingStats,
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


def _unpriced_model_warnings(trials: list[TrialResult], cfg: RepoBenchConfig) -> list[str]:
    """Issue #17: a target whose trials carry no cost silently disables the
    economic comparison. Say so once per target instead of staying quiet —
    a cost source only silences the warning when it could actually produce a
    cost: a user pricing rule, or a catalog entry WITH reported usage (a
    catalog-known model whose harness never reported tokens is still costless)."""
    trials_by_target: dict[str, list[TrialResult]] = {}
    for trial in trials:
        trials_by_target.setdefault(trial.target_id, []).append(trial)
    warnings: list[str] = []
    for target_id in sorted(trials_by_target):
        target_trials = trials_by_target[target_id]
        if any(t.usage is not None and t.cost_usd is not None for t in target_trials):
            continue
        model = next((t.model for t in target_trials if t.model), None)
        if model is not None and model in cfg.pricing:
            continue
        if pricing_catalog.lookup(model) is not None and any(
            t.usage is not None for t in target_trials
        ):
            continue
        shown_model = model or "(harness default model)"
        warnings.append(
            f"target {target_id} (model {shown_model}) reported no cost and has no "
            "usable pricing — economic comparison disabled for it"
        )
    return warnings


def _test_tampering_stats(trials: list[TrialResult]) -> TestTamperingStats | None:
    """issue #18: aggregate the reward-hacking signal across the run. Populated
    only when at least one trial's final diff touched test files (None otherwise
    — same gating as `reliability`); tampered-but-passing trials stay SOLVED, so
    this section is the only place the finding surfaces (PRD §42)."""
    flagged = [t for t in trials if t.tampered_tests]
    if not flagged:
        return None
    trials_by_target: dict[str, int] = {}
    for trial in trials:
        trials_by_target[trial.target_id] = trials_by_target.get(trial.target_id, 0) + 1
    by_target: dict[str, int] = {}
    paths_by_target: dict[str, list[str]] = {}
    for trial in flagged:
        by_target[trial.target_id] = by_target.get(trial.target_id, 0) + 1
        target_paths = paths_by_target.setdefault(trial.target_id, [])
        for path in trial.tampered_tests:
            if path not in target_paths:
                target_paths.append(path)
        paths_by_target[trial.target_id] = sorted(target_paths)[:TAMPERED_PATHS_CAP]
    return TestTamperingStats(
        flagged_trials=len(flagged),
        total_trials=len(trials),
        by_target=by_target,
        trials_by_target=trials_by_target,
        paths_by_target=paths_by_target,
        paths=sorted({p for t in flagged for p in t.tampered_tests})[:TAMPERED_PATHS_CAP],
    )


def _tamper_warnings(trials: list[TrialResult]) -> list[str]:
    """issue #18: one warning when any trial touched test files, mirroring the
    _harness_version_warnings style — findings are never silent."""
    flagged = sum(1 for trial in trials if trial.tampered_tests)
    if not flagged:
        return []
    return [
        f"{flagged} trial(s) touched test files after the agent ran — "
        "reward-hacking signal (see Reward hacking section)"
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

    # Multi-rollout reliability (issue #13): only computed when the run actually
    # used more than one rollout; k is the max rollout index seen.
    reliability: dict[str, TargetReliability] | None = None
    if any(trial.rollout > 1 for trial in trials):
        reliability = reliability_stats(trials, k=max(trial.rollout for trial in trials))

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
    warnings.extend(_unpriced_model_warnings(trials, cfg))
    warnings.extend(_tamper_warnings(trials))
    # Reward-hacking signal (issue #18); None unless some trial touched tests.
    test_tampering = _test_tampering_stats(trials)

    # Single source of truth (PRD §89): the immutable manifest records the
    # repository the benchmark was built from. GitRepo is only a fallback for
    # runs whose benchmark row has no loadable manifest.
    repository = None
    stored_manifest = load_stored_manifest(benchmark) if benchmark else None
    if stored_manifest is not None:
        repository = stored_manifest.repository
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
        reliability=reliability,
        test_tampering=test_tampering,
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


# --------------------------------------------- compare across runs (issue #14)


@dataclass
class CompareTargetDelta:
    """Per-target A → B regression delta (PRD §149, issue #14).

    diff/CI are B − A in percentage points; costs are the aggregate
    `effective_cost_usd` of each run's deduped trials.
    """

    target_id: str
    n_tasks_a: int
    n_tasks_b: int
    common_tasks: int
    rate_a: float
    rate_b: float
    diff_pp: float
    ci_lo_pp: float
    ci_hi_pp: float
    conclusive: bool
    cost_a: float | None
    cost_b: float | None
    cost_delta_pct: float | None


@dataclass
class CompareSegmentDelta:
    """One segment's A → B rate delta; segments only when both runs hit them."""

    segment: str
    rate_a: float
    rate_b: float
    diff_pp: float
    n_a: int
    n_b: int


@dataclass
class CompareOutcome:
    """Everything `repobench compare` renders (PRD §149, issue #14)."""

    run_a: str
    run_b: str
    benchmark_id: str | None
    targets: list[CompareTargetDelta]
    segments: dict[str, list[CompareSegmentDelta]]
    warnings: list[str]
    tasks_only_a: int
    tasks_only_b: int


# PRD §149 compares overall plus subsystem-level drift; task_type is the second
# slice. The remaining dimensions stay report-only (SEGMENT_DIMENSIONS).
COMPARE_SEGMENT_DIMENSIONS = ("subsystem", "task_type")


def _latest_trials_by_target(trials: list[TrialResult]) -> dict[str, list[TrialResult]]:
    """Deduped trials per target: ONE trial per (task, target) — the last stored
    attempt wins.

    list_trials returns rows ordered by created_at, so keeping the last
    occurrence in that order keeps the newest verdict. Needed since issue #13:
    after `--retry-failed` a (task, target) pair can hold several stored
    attempts, and a comparison must weigh each task exactly once (issue #14).
    """
    latest: dict[tuple[str, str], TrialResult] = {}
    for trial in trials:
        latest[(trial.target_id, trial.task_id)] = trial
    by_target: dict[str, list[TrialResult]] = {}
    for (target_id, _task_id), trial in latest.items():
        by_target.setdefault(target_id, []).append(trial)
    for per_target in by_target.values():
        per_target.sort(key=lambda trial: trial.task_id)
    return by_target


def _pooled_segment_rates(
    trials_by_target: dict[str, list[TrialResult]],
    tasks: dict[str, TaskMetadata],
    dimension: str,
) -> dict[str, tuple[float, int]]:
    """segment -> (rate, n) pooled across ALL targets of one run (issue #14).

    segment_breakdown is per target; compare pools every target's deduped
    trials of the run, so a segment rate is solved/total over the whole run —
    the per-run aggregate a regression decision is made from.
    """
    pooled: dict[str, list[int]] = {}  # segment -> [solved, n]
    for per_target in trials_by_target.values():
        for segments in segment_breakdown(per_target, tasks, dimension).values():
            for segment, stat in segments.items():
                acc = pooled.setdefault(segment, [0, 0])
                acc[0] += stat.solved
                acc[1] += stat.n
    return {
        segment: (solved / n, n) if n else (0.0, 0)
        for segment, (solved, n) in pooled.items()
    }


def _load_run_manifest(paths: ProjectPaths, run_id: str) -> dict | None:
    """runs/<id>/manifest.json, or None when absent/unreadable — fingerprint
    comparison degrades to a warning, never a crash (issue #14)."""
    manifest_path = paths.run_dir(run_id) / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None


def _fingerprint_warnings(paths: ProjectPaths, run_a_id: str, run_b_id: str) -> list[str]:
    """Reproducibility warnings from the two runs' manifests (issue #14, task 2).

    Compares each shared target's config_hash and each shared harness's version
    across runs/<id>/manifest.json; a missing or unreadable manifest yields a
    warning instead of aborting the comparison.
    """
    manifest_a = _load_run_manifest(paths, run_a_id)
    manifest_b = _load_run_manifest(paths, run_b_id)
    warnings: list[str] = []
    for run_id, manifest in ((run_a_id, manifest_a), (run_b_id, manifest_b)):
        if manifest is None:
            warnings.append(
                f"run {run_id} has no reproducibility manifest — "
                "fingerprint comparison unavailable"
            )
    if manifest_a is None or manifest_b is None:
        return warnings

    def config_hashes(manifest: dict) -> dict[str, str | None]:
        return {
            entry.get("definition", {}).get("id"): entry.get("config_hash")
            for entry in manifest.get("targets", [])
        }

    hashes_a, hashes_b = config_hashes(manifest_a), config_hashes(manifest_b)
    for target_id in sorted(set(hashes_a) & set(hashes_b)):
        if hashes_a[target_id] != hashes_b[target_id]:
            warnings.append(
                f"target {target_id} config changed between runs — "
                "results are not directly comparable"
            )
    versions_a = manifest_a.get("harnesses", {})
    versions_b = manifest_b.get("harnesses", {})
    for name in sorted(set(versions_a) & set(versions_b)):
        if versions_a[name] != versions_b[name]:
            warnings.append(
                f"harness {name} version changed between runs "
                f"({versions_a[name]} → {versions_b[name]})"
            )
    return warnings


def build_compare(
    root: Path, storage: Storage, run_a_id: str, run_b_id: str
) -> CompareOutcome:
    """Assemble the A → B regression comparison (PRD §149, issue #14).

    Rates and costs come from each run's deduped trials (one per task, last
    attempt); the paired bootstrap is fed (B, A) so diff/CI are B − A. Segment
    rates pool all targets of a run per segment and cover only segments present
    in both runs. Fingerprint and task-set drift become warnings — never abort.
    """
    run_a = _resolve_run(storage, run_a_id)
    run_b = _resolve_run(storage, run_b_id)

    benchmark_a = run_a.get("benchmark_id")
    benchmark_b = run_b.get("benchmark_id")
    if not benchmark_a or not benchmark_b or benchmark_a != benchmark_b:
        raise UsageError(
            "compare needs two runs of the same benchmark — "
            f"{run_a_id} ran {benchmark_a or '(none)'}, "
            f"{run_b_id} ran {benchmark_b or '(none)'}"
        )

    # Wave-1 pattern: the seed stored with the compared run (B) drives the
    # bootstrap; 42 is the analysis default when the run recorded none.
    run_b_config: dict = {}
    if run_b.get("config_json"):
        try:
            run_b_config = json.loads(run_b["config_json"])
        except Exception:
            run_b_config = {}
    bootstrap_seed = run_b_config.get("bootstrap_seed", 42)

    by_target_a = _latest_trials_by_target(storage.list_trials(run_a_id))
    by_target_b = _latest_trials_by_target(storage.list_trials(run_b_id))
    metrics_a = aggregate_trials([t for ts in by_target_a.values() for t in ts])
    metrics_b = aggregate_trials([t for ts in by_target_b.values() for t in ts])

    targets: list[CompareTargetDelta] = []
    for target_id in sorted(set(by_target_a) | set(by_target_b)):
        trials_a = by_target_a.get(target_id, [])
        trials_b = by_target_b.get(target_id, [])
        # (B, A) argument order: paired_bootstrap diffs first-minus-second, so
        # this yields the command's B − A delta convention.
        boot = paired_bootstrap(trials_b, trials_a, seed=bootstrap_seed)
        metrics_side_a = metrics_a.get(target_id)
        metrics_side_b = metrics_b.get(target_id)
        cost_a = metrics_side_a.effective_cost_usd if metrics_side_a else None
        cost_b = metrics_side_b.effective_cost_usd if metrics_side_b else None
        if cost_a is not None and cost_b is not None and cost_a > 0:
            cost_delta_pct: float | None = (cost_b - cost_a) / cost_a * 100
        else:
            cost_delta_pct = None
        common = {t.task_id for t in trials_a} & {t.task_id for t in trials_b}
        targets.append(
            CompareTargetDelta(
                target_id=target_id,
                n_tasks_a=len(trials_a),
                n_tasks_b=len(trials_b),
                common_tasks=len(common),
                # A side with zero trials contributes rate 0.0 and an empty
                # intersection — bootstrap then skips the CI (conclusive False).
                rate_a=metrics_side_a.solve_rate if metrics_side_a else 0.0,
                rate_b=metrics_side_b.solve_rate if metrics_side_b else 0.0,
                diff_pp=boot["mean_diff_pp"],
                ci_lo_pp=boot["ci_lo_pp"],
                ci_hi_pp=boot["ci_hi_pp"],
                conclusive=boot["conclusive"],
                cost_a=cost_a,
                cost_b=cost_b,
                cost_delta_pct=cost_delta_pct,
            )
        )

    # Segment rates pool all targets per run; task metadata joins from disk
    # packages with the tasks-table fallback (same pattern as build_report_data).
    task_ids = sorted(
        {t.task_id for ts in [*by_target_a.values(), *by_target_b.values()] for t in ts}
    )
    tasks = load_task_metadata(ProjectPaths(root), storage, task_ids)
    segments: dict[str, list[CompareSegmentDelta]] = {}
    for dimension in COMPARE_SEGMENT_DIMENSIONS:
        rates_a = _pooled_segment_rates(by_target_a, tasks, dimension)
        rates_b = _pooled_segment_rates(by_target_b, tasks, dimension)
        segments[dimension] = [
            CompareSegmentDelta(
                segment=segment,
                rate_a=rates_a[segment][0],
                rate_b=rates_b[segment][0],
                diff_pp=(rates_b[segment][0] - rates_a[segment][0]) * 100,
                n_a=rates_a[segment][1],
                n_b=rates_b[segment][1],
            )
            for segment in sorted(set(rates_a) & set(rates_b))
        ]

    tasks_a = {t.task_id for ts in by_target_a.values() for t in ts}
    tasks_b = {t.task_id for ts in by_target_b.values() for t in ts}
    tasks_only_a = len(tasks_a - tasks_b)
    tasks_only_b = len(tasks_b - tasks_a)
    warnings = _fingerprint_warnings(ProjectPaths(root), run_a_id, run_b_id)
    if tasks_only_a:
        warnings.append(f"{tasks_only_a} tasks in A missing from B")
    if tasks_only_b:
        warnings.append(f"{tasks_only_b} tasks in B missing from A")

    return CompareOutcome(
        run_a=run_a_id,
        run_b=run_b_id,
        benchmark_id=benchmark_a,
        targets=targets,
        segments=segments,
        warnings=warnings,
        tasks_only_a=tasks_only_a,
        tasks_only_b=tasks_only_b,
    )
