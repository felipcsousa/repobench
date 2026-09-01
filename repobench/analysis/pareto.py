"""Pareto frontier over quality vs cost (or time when cost is unavailable) (PRD §106)."""

from __future__ import annotations

import math

import pydantic

from repobench.analysis.metrics import TargetMetrics


class ParetoResult(pydantic.BaseModel):
    frontier: list[str]
    axes: str  # "quality-cost" | "quality-time"


def pareto_frontier(metrics: list[TargetMetrics]) -> ParetoResult:
    """Targets not dominated on (maximize solve_rate, minimize cost|time).

    The cost axis is used only when every target reports an effective_cost_usd
    (one honest cost figure per target); otherwise time_p50_ms is the second
    axis. A missing time value counts as the worst value.
    """
    if not metrics:
        return ParetoResult(frontier=[], axes="quality-cost")

    use_cost = all(m.effective_cost_usd is not None for m in metrics)

    def axis_value(m: TargetMetrics) -> float:
        if use_cost:
            # The gate above guarantees a cost figure for every target.
            assert m.effective_cost_usd is not None
            return m.effective_cost_usd
        return float(m.time_p50_ms) if m.time_p50_ms is not None else math.inf

    def dominated_by(a: TargetMetrics, b: TargetMetrics) -> bool:
        if a is b:
            return False
        quality_ok = b.solve_rate >= a.solve_rate
        axis_ok = axis_value(b) <= axis_value(a)
        strictly_better = b.solve_rate > a.solve_rate or axis_value(b) < axis_value(a)
        return quality_ok and axis_ok and strictly_better

    frontier = [
        m.target_id
        for m in metrics
        if not any(dominated_by(m, other) for other in metrics)
    ]
    return ParetoResult(frontier=frontier, axes="quality-cost" if use_cost else "quality-time")
