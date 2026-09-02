"""Benchmark Health composite score and honest warnings (PRD §86-87)."""

from __future__ import annotations

import statistics
from datetime import datetime

import pydantic

from repobench.benchmark.coverage import CoverageReport
from repobench.core.types import TaskMetadata, utcnow

# Component weights: representativeness/validation/leakage/recency/diversity
# are fixed by the PRD (§86) and must sum to 1. Issue #19 inserts the
# verifier-strength component by rebalancing the §86 weights (validation
# .25 → .15; the freed .10 funds verifier strength) — a documented deviation
# from §86, still summing to 1.
_WEIGHT_REPRESENTATIVENESS = 0.40
_WEIGHT_VALIDATION = 0.15
_WEIGHT_VERIFIER_STRENGTH = 0.10
_WEIGHT_LEAKAGE = 0.15
_WEIGHT_RECENCY = 0.10
_WEIGHT_DIVERSITY = 0.10


class HealthReport(pydantic.BaseModel):
    """All components 0-100; overall is the weighted composite.

    verifier_strength is None only for benchmarks whose persisted health
    predates issue #19 — renderers must handle None instead of inventing a
    score; new computations always set it.
    """

    representativeness: int
    validation_confidence: int
    verifier_strength: int | None = None
    leakage_resistance: int
    recency: int
    diversity: int
    overall: int
    warnings: list[str]


def _recency_score(
    tasks: list[TaskMetadata], lookback_days: int, now: datetime
) -> int:
    ages_days = [
        (now - task.created_at).total_seconds() / 86400
        for task in tasks
        if task.created_at is not None
    ]
    if not ages_days:
        # No timestamps means no signal — report a neutral midpoint, not a guess.
        return 50
    median_age_days = statistics.median(ages_days)
    if lookback_days <= 0:
        return 0
    return round(100 * max(0.0, 1 - median_age_days / lookback_days))


def _diversity_score(tasks: list[TaskMetadata]) -> int:
    n = len(tasks)
    if n == 0:
        return 0
    subsystem_ratio = len({t.assessment.subsystem for t in tasks}) / min(n, 12)
    type_ratio = len({t.assessment.task_type for t in tasks}) / min(n, 8)
    return round(min(100.0, max(0.0, (subsystem_ratio + type_ratio) / 2 * 100)))


# PRD §51: public repositories cannot rule out contamination — the warning
# travels inside HealthReport so every report built from the benchmark carries it.
PUBLIC_REPOSITORY_WARNING = (
    "Public repository: the tested model may have seen this repository, its "
    "issues or the solution — results measure practical performance, not "
    "contamination-free capability (PRD §51)"
)


def compute_health(
    *,
    coverage: CoverageReport,
    all_checks_passed_ratio: float,
    leakage_score: int,
    tasks: list[TaskMetadata],
    universe_counts: dict[str, int] | None = None,
    lookback_days: int = 180,
    public_repository: bool = False,
    now: datetime | None = None,
    flaky_tasks: list[str] | None = None,
    total_validated_tasks: int = 0,
    brittle_warnings: list[str] | None = None,
) -> HealthReport:
    """Composite health (PRD §86) with honest limitations surfaced as warnings (PRD §87).

    Health never replaces hard gates; the network-isolation warning is unconditional
    because Local Mode executes host-native (PRD §87).

    The last three parameters form the verifier-strength group (issue #19), kept
    cohesive because they describe one component: `flaky_tasks` are task ids whose
    validation outcome flipped between builds (the score input), scored as a ratio
    over `total_validated_tasks` (the distinct tasks the history can judge — the
    caller derives both from the same history scan), and `brittle_warnings` are
    linter findings over verifier diffs that only ever warn (heuristic, prone to
    false positives — never a score input).
    """
    now = now or utcnow()

    representativeness = coverage.overall
    validation_confidence = round(all_checks_passed_ratio * 100)
    recency = _recency_score(tasks, lookback_days, now)
    diversity = _diversity_score(tasks)

    # No history is no signal: with total_validated_tasks == 0 the ratio is 0.0
    # and a benchmark whose tasks never flip-flopped scores a full 100.
    flaky = flaky_tasks or []
    flaky_ratio = (
        len(flaky) / total_validated_tasks if total_validated_tasks > 0 else 0.0
    )
    verifier_strength = round(100 * (1 - flaky_ratio))

    overall = round(
        _WEIGHT_REPRESENTATIVENESS * representativeness
        + _WEIGHT_VALIDATION * validation_confidence
        + _WEIGHT_VERIFIER_STRENGTH * verifier_strength
        + _WEIGHT_LEAKAGE * leakage_score
        + _WEIGHT_RECENCY * recency
        + _WEIGHT_DIVERSITY * diversity
    )

    warnings = ["No network isolation (host-native execution)"]
    if public_repository:
        warnings.append(PUBLIC_REPOSITORY_WARNING)
    if flaky:
        warnings.append(
            f"{len(flaky)} task(s) with flaky validation history "
            "(outcome flipped between builds)"
        )
    warnings.extend(brittle_warnings or [])
    if universe_counts:
        total_universe = sum(universe_counts.values())
        n_sample = len(tasks)
        if total_universe > 0 and n_sample > 0:
            sample_counts: dict[str, int] = {}
            for task in tasks:
                key = task.assessment.task_type.value
                sample_counts[key] = sample_counts.get(key, 0) + 1
            for task_type, count in universe_counts.items():
                universe_share = count / total_universe
                sample_share = sample_counts.get(task_type, 0) / n_sample
                if universe_share - sample_share > 0.10:
                    warnings.append(f"{task_type} work underrepresented")

    return HealthReport(
        representativeness=representativeness,
        validation_confidence=validation_confidence,
        verifier_strength=verifier_strength,
        leakage_resistance=leakage_score,
        recency=recency,
        diversity=diversity,
        overall=overall,
        warnings=warnings,
    )
