"""Per-target metric aggregation over trials (PRD §101, §109-110).

Constraint (PRD §53-54): usage/cost numbers are only aggregated from what the
harness actually reported — missing data propagates to None, never invented.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pydantic

from repobench.analysis.stats import wilson_ci
from repobench.core.types import TaskMetadata, TrialOutcome, TrialResult

_LOW_SAMPLE_THRESHOLD = 5  # PRD §110: segments with n < 5 are descriptive only
_ERROR_OUTCOMES = (
    TrialOutcome.HARNESS_ERROR,
    TrialOutcome.SETUP_ERROR,
    TrialOutcome.VERIFIER_ERROR,
)


class SegmentStat(pydantic.BaseModel):
    n: int
    solved: int
    rate: float
    low_sample: bool


class TargetMetrics(pydantic.BaseModel):
    target_id: str
    n: int
    solved: int
    solve_rate: float
    time_p50_ms: int | None
    time_p90_ms: int | None
    timeouts: int
    errors: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_cost_usd: float | None
    cost_source: str | None
    cost_per_solve_usd: float | None
    # The honest cost of one target: cost_per_solve_usd when reported, else
    # total_cost_usd; None when cost went unreported entirely. The one accessor
    # cost-based analysis (Pareto, recommendation) must go through.
    effective_cost_usd: float | None
    mean_files_changed: float | None
    # Wilson 95% interval on solve_rate, computed once here in aggregation so
    # every report (terminal and JSON) carries and renders the same interval.
    wilson_lo: float | None
    wilson_hi: float | None
    # Partial credit (Onda 4): mean per-test ratio over the trials that carry
    # hidden-verifier counts. Honesty rule (PRD): a number is never invented —
    # when no trial reported counts every derived value stays None/0, and
    # tests_partial_n always discloses how many trials back the mean.
    tests_partial: float | None = None  # mean of passed/(total-skipped) per trial
    tests_partial_n: int = 0  # trials contributing to the mean
    tests_mean_passed: float | None = None  # mean of passed counts (same trials)
    tests_mean_denominator: float | None = None  # mean of (total - skipped)


def nearest_rank(sorted_values: Sequence[float], percentile: float) -> float | None:
    """Nearest-rank percentile (percentile in [0, 100]) on a pre-sorted list.

    The one shared percentile implementation; None for empty input.
    """
    if not sorted_values:
        return None
    rank = max(1, min(len(sorted_values), math.ceil(percentile / 100 * len(sorted_values))))
    return sorted_values[rank - 1]


def _token_sum(trials: list[TrialResult], attr: str) -> int | None:
    """Sum of present usage values; None when no trial reported that field at all."""
    values = [
        getattr(trial.usage, attr)
        for trial in trials
        if trial.usage is not None and getattr(trial.usage, attr) is not None
    ]
    return sum(values) if values else None


def _aggregate_one(target_id: str, trials: list[TrialResult]) -> TargetMetrics:
    n = len(trials)
    solved = sum(1 for t in trials if t.outcome == TrialOutcome.SOLVED)
    durations = sorted(t.duration_ms for t in trials)
    timeouts = sum(1 for t in trials if t.outcome == TrialOutcome.TIMEOUT)
    errors = sum(1 for t in trials if t.outcome in _ERROR_OUTCOMES)

    # Cost counts every trial that carries usage or an explicit cost; the total
    # is only reported when every counted trial reported a cost (honest totals).
    cost_trials = [t for t in trials if t.usage is not None or t.cost_usd is not None]
    if cost_trials and all(t.cost_usd is not None for t in cost_trials):
        total_cost = sum(t.cost_usd for t in cost_trials)
        sources = {t.cost_source for t in cost_trials if t.cost_source is not None}
        if len(sources) == 1:
            cost_source: str | None = next(iter(sources))
        elif sources:
            cost_source = "MIXED"
        else:
            cost_source = None
    else:
        total_cost = None
        cost_source = None

    files_changed = [t.files_changed for t in trials if t.files_changed is not None]

    # Partial credit (Onda 4): only trials carrying all four per-test counts are
    # data; a fully-skipped suite (denominator = total - skipped <= 0) ran no
    # real test and is excluded too. Missing data propagates to None — never
    # invented into a mean (PRD honesty rule, same as cost/tokens above).
    counted = [
        t
        for t in trials
        if t.tests_passed is not None
        and t.tests_failed is not None
        and t.tests_skipped is not None
        and t.tests_total is not None
    ]
    contributing = [t for t in counted if t.tests_total - t.tests_skipped > 0]
    if contributing:
        passed = [t.tests_passed for t in contributing]
        denominators = [t.tests_total - t.tests_skipped for t in contributing]
        tests_partial = sum(p / d for p, d in zip(passed, denominators)) / len(contributing)
        tests_mean_passed = sum(passed) / len(contributing)
        tests_mean_denominator = sum(denominators) / len(contributing)
        tests_partial_n = len(contributing)
    else:
        tests_partial = None
        tests_mean_passed = None
        tests_mean_denominator = None
        tests_partial_n = 0

    cost_per_solve = total_cost / solved if total_cost is not None and solved > 0 else None
    wilson_lo, wilson_hi = wilson_ci(solved, n)

    return TargetMetrics(
        target_id=target_id,
        n=n,
        solved=solved,
        solve_rate=solved / n,
        time_p50_ms=nearest_rank(durations, 50),
        time_p90_ms=nearest_rank(durations, 90),
        timeouts=timeouts,
        errors=errors,
        total_input_tokens=_token_sum(trials, "input_tokens"),
        total_output_tokens=_token_sum(trials, "output_tokens"),
        total_cost_usd=total_cost,
        cost_source=cost_source,
        cost_per_solve_usd=cost_per_solve,
        effective_cost_usd=cost_per_solve if cost_per_solve is not None else total_cost,
        mean_files_changed=(
            sum(files_changed) / len(files_changed) if files_changed else None
        ),
        wilson_lo=wilson_lo,
        wilson_hi=wilson_hi,
        tests_partial=tests_partial,
        tests_partial_n=tests_partial_n,
        tests_mean_passed=tests_mean_passed,
        tests_mean_denominator=tests_mean_denominator,
    )


def aggregate_trials(trials: list[TrialResult]) -> dict[str, TargetMetrics]:
    grouped: dict[str, list[TrialResult]] = {}
    for trial in trials:
        grouped.setdefault(trial.target_id, []).append(trial)
    return {target_id: _aggregate_one(target_id, grouped[target_id]) for target_id in sorted(grouped)}


_SEGMENT_DIMENSIONS = ("task_type", "subsystem", "complexity", "instruction_confidence")


def _segment_value(task, dimension: str) -> str:
    if dimension == "task_type":
        return task.assessment.task_type.value
    if dimension == "subsystem":
        return task.assessment.subsystem
    if dimension == "complexity":
        return task.assessment.complexity.value
    if dimension == "instruction_confidence":
        return task.assessment.instruction_confidence
    raise ValueError(f"unknown segmentation dimension: {dimension!r}")


def segment_breakdown(
    trials: list[TrialResult],
    tasks: dict[str, TaskMetadata],
    dimension: str,
) -> dict[str, dict[str, SegmentStat]]:
    """target -> segment -> stat for one dimension (PRD §109-110).

    Trials whose task_id is absent from `tasks` are skipped; segments with
    n < 5 carry low_sample=True and are descriptive only (PRD §110).
    """
    if dimension not in _SEGMENT_DIMENSIONS:
        raise ValueError(f"unknown segmentation dimension: {dimension!r}")
    grouped: dict[str, dict[str, list[TrialResult]]] = {}
    for trial in trials:
        task = tasks.get(trial.task_id)
        if task is None:
            continue
        segment = _segment_value(task, dimension)
        grouped.setdefault(trial.target_id, {}).setdefault(segment, []).append(trial)

    result: dict[str, dict[str, SegmentStat]] = {}
    for target_id in sorted(grouped):
        segments: dict[str, SegmentStat] = {}
        for segment in sorted(grouped[target_id]):
            seg_trials = grouped[target_id][segment]
            n = len(seg_trials)
            solved = sum(1 for t in seg_trials if t.outcome == TrialOutcome.SOLVED)
            segments[segment] = SegmentStat(
                n=n,
                solved=solved,
                rate=solved / n,
                low_sample=n < _LOW_SAMPLE_THRESHOLD,
            )
        result[target_id] = segments
    return result
