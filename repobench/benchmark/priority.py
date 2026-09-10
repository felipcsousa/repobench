"""Adaptive validation priority: the coverage-aware candidate queue (PRD §25-29).

Why score at all (PRD §13): real validation costs minutes per candidate
(install, build, several test executions), so the build ranks candidates with
the cheap mining assessment first and spends the expensive validation in that
order. The queue balances two honest signals (PRD §28):

    ValidationPriority = 0.65 x CandidateScore + 0.35 x CoverageNeed

CoverageNeed measures how much a candidate would fill dimensions that are still
under-represented among the VALID tasks so far (PRD §27-29), so an integration
candidate is boosted exactly when the benchmark is still missing integration
work — candidate quality never decides correctness, only what to try first
(PRD §12, §81).

Everything here is pure and deterministic (PRD §72): no randomness, no I/O,
sorted tie-breaks — repeated calls with the same inputs always agree.
"""

from __future__ import annotations

from repobench.benchmark.sampling import (
    DIMENSION_NAMES,
    dimension_values,
    distribution_of,
    tv_distance,
)
from repobench.config import BenchmarkDimensions
from repobench.core.types import Assessment, CandidateClass, CandidateInfo

# Adaptive priority weights (PRD §28): quality dominates, coverage breaks ties
# toward representativeness.
SCORE_WEIGHT = 0.65
NEED_WEIGHT = 0.35

# Validation order (PRD §25): HIGH before MEDIUM before EXPERIMENTAL. Unscored
# candidates (hard_caps mining, or adaptive low-priority output) rank as
# medium — an unknown score is never treated as better than a known one.
_CLASS_RANKS: dict[CandidateClass, int] = {
    CandidateClass.HIGH_CONFIDENCE: 0,
    CandidateClass.MEDIUM_CONFIDENCE: 1,
    CandidateClass.EXPERIMENTAL: 2,
}
UNSCORED_CLASS_RANK = 1


def _universe_distributions(universe: list[CandidateInfo]) -> list[dict[str, float]]:
    return [
        distribution_of([dimension_values(c.assessment)[i] for c in universe])
        for i in range(len(DIMENSION_NAMES))
    ]


def _valid_distributions(valid: list[Assessment]) -> list[dict[str, float]]:
    return [
        distribution_of([dimension_values(a)[i] for a in valid])
        for i in range(len(DIMENSION_NAMES))
    ]


def coverage_need(
    candidate_assessment: Assessment,
    validated_assessments: list[Assessment],
    universe: list[CandidateInfo],
    weights: BenchmarkDimensions,
) -> float:
    """How much this candidate helps fill under-represented dimensions, 0-1
    (PRD §27-29).

    For each dimension d in DIMENSION_NAMES: u_d is the share distribution of
    the Workload Universe (`universe`), v_d the share distribution of the VALID
    tasks so far — an empty validated set leaves every universe share as
    deficit. Per key: deficit_d(key) = max(0, u_d(key) - v_d(key)); the
    candidate's need is

        need = sum_d weight_d * deficit_d(candidate's key in d)

    clamped to [0, 1] (weights sum to 1 and a single key's deficit is at most
    1). Deterministic. PRD §28 example: with bugfix already validated enough
    and integration missing, an integration candidate carries a positive
    task-type deficit while the bugfix's is zero — the integration candidate
    gets the coverage boost.
    """
    dim_weights = (weights.task_type, weights.subsystem, weights.complexity)
    universe_dists = _universe_distributions(universe)
    valid_dists = _valid_distributions(validated_assessments)
    keys = dimension_values(candidate_assessment)
    need = 0.0
    for i, weight in enumerate(dim_weights):
        deficit = max(
            0.0,
            universe_dists[i].get(keys[i], 0.0) - valid_dists[i].get(keys[i], 0.0),
        )
        need += weight * deficit
    return min(max(need, 0.0), 1.0)


def top_deficit(
    validated_assessments: list[Assessment],
    universe: list[CandidateInfo],
    weights: BenchmarkDimensions,
) -> tuple[str, str] | None:
    """The (dimension, key) with the largest weighted coverage deficit (PRD §28,
    §60) — the gap validation should chase next — or None when the universe is
    empty or nothing is under-represented. Deterministic: ties keep the first
    (dimension, key) in DIMENSION_NAMES / lexicographic key order."""
    if not universe:
        return None
    dim_weights = (weights.task_type, weights.subsystem, weights.complexity)
    universe_dists = _universe_distributions(universe)
    valid_dists = _valid_distributions(validated_assessments)
    best: tuple[str, str] | None = None
    best_value = 0.0
    for i, weight in enumerate(dim_weights):
        for key in sorted(universe_dists[i]):
            deficit = weight * max(
                0.0,
                universe_dists[i].get(key, 0.0) - valid_dists[i].get(key, 0.0),
            )
            if deficit > best_value:
                best_value = deficit
                best = (DIMENSION_NAMES[i], key)
    return best


def validation_priority(candidate_score: float | None, need: float) -> float:
    """Adaptive validation priority (PRD §28): 0.65 x CandidateScore +
    0.35 x CoverageNeed, rounded to 2 decimals. A None score (unscored
    candidate) counts as 0 — missing evidence never invents quality (PRD §53-54).
    """
    return round(SCORE_WEIGHT * (candidate_score or 0.0) + NEED_WEIGHT * need, 2)


def class_rank(candidate: CandidateInfo) -> int:
    """Validation order of the candidate's class (PRD §25): HIGH_CONFIDENCE=0,
    MEDIUM_CONFIDENCE=1, EXPERIMENTAL=2; None (hard_caps / unscored) is treated
    as medium."""
    return _CLASS_RANKS.get(candidate.assessment.candidate_class, UNSCORED_CLASS_RANK)


def order_pool(
    pool: list[CandidateInfo],
    universe: list[CandidateInfo],
    weights: BenchmarkDimensions,
    validated_so_far: list[Assessment],
) -> list[CandidateInfo]:
    """Initial validation order before any validation happened (PRD §25, §28-29):
    by class (HIGH → MEDIUM → EXPERIMENTAL, unscored as medium), then by
    validation priority descending, then by candidate_id — a total, stable,
    deterministic order (PRD §72)."""
    def sort_key(candidate: CandidateInfo) -> tuple[int, float, str]:
        need = coverage_need(candidate.assessment, validated_so_far, universe, weights)
        return (
            class_rank(candidate),
            -validation_priority(candidate.assessment.candidate_score, need),
            candidate.candidate_id,
        )

    return sorted(pool, key=sort_key)


def representativeness(
    valid: list[Assessment],
    universe: list[CandidateInfo],
    weights: BenchmarkDimensions,
) -> float:
    """Weighted composite representativeness of the valid set against the
    Workload Universe, 0-100 (PRD §61): 100 * (1 - weighted TV distance) over
    the three benchmark dimensions.

    CoverageReport (repobench/benchmark/coverage.py) only exposes rounded
    per-dimension scores, so the build computes this composite here when it
    needs to compare against benchmark.min_representativeness — a number it
    actually computed, never a claim. An empty valid set is maximally
    unrepresentative: 0 (the tv_distance convention)."""
    if not universe or not valid:
        return 0.0
    dim_weights = (weights.task_type, weights.subsystem, weights.complexity)
    total_weight = sum(dim_weights)
    universe_dists = _universe_distributions(universe)
    valid_dists = _valid_distributions(valid)
    weighted_tv = sum(
        (weight / total_weight if total_weight > 0 else 1 / 3)
        * tv_distance(valid_dists[i], universe_dists[i])
        for i, weight in enumerate(dim_weights)
    )
    return 100.0 * (1.0 - weighted_tv)
