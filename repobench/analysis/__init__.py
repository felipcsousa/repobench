"""Analysis of run results: metrics, statistics, Pareto, recommendation (PRD §101-110)."""

from repobench.analysis.metrics import (
    SegmentStat,
    TargetMetrics,
    aggregate_trials,
    segment_breakdown,
)
from repobench.analysis.pareto import ParetoResult, pareto_frontier
from repobench.analysis.recommendation import (
    NO_COST_REASON,
    Recommendation,
    recommend,
)
from repobench.analysis.stats import paired_bootstrap, wilson_ci

__all__ = [
    "NO_COST_REASON",
    "ParetoResult",
    "Recommendation",
    "SegmentStat",
    "TargetMetrics",
    "aggregate_trials",
    "paired_bootstrap",
    "pareto_frontier",
    "recommend",
    "segment_breakdown",
    "wilson_ci",
]
