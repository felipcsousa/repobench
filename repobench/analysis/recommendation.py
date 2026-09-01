"""Cost-effective target recommendation policy (PRD §107-108).

Policy: identify best observed quality, collect targets without a conclusive
difference against it, then pick the cheapest per verified solve inside that
set. The economic recommendation is made only when EVERY member of that set
reports an effective cost — priced targets are never compared against unpriced
ones. Otherwise no economic recommendation is made — never fake precision
(PRD §108).
"""

from __future__ import annotations

import math

import pydantic

from repobench.analysis.metrics import TargetMetrics

NO_COST_REASON = (
    "Economic recommendation unavailable (subscription-backed or unreported costs)"
)


class Recommendation(pydantic.BaseModel):
    best_quality_target: str | None
    candidates_not_worse: list[str]
    recommended: str | None
    reason: str


def _time_of(m: TargetMetrics) -> float:
    return float(m.time_p50_ms) if m.time_p50_ms is not None else math.inf


def recommend(
    metrics: dict[str, TargetMetrics],
    comparisons: dict[tuple[str, str], dict],
) -> Recommendation:
    """Apply the default cost-effective-target policy (PRD §107).

    `comparisons` maps (best_id, other_id) -> paired_bootstrap dict. A missing
    comparison counts as inconclusive: excluding a candidate requires
    conclusive evidence it is worse (PRD §105).
    """
    if not metrics:
        return Recommendation(
            best_quality_target=None,
            candidates_not_worse=[],
            recommended=None,
            reason="No target metrics available; nothing to recommend.",
        )

    # 1) Best observed quality: highest solve rate, tie broken by lower time.
    best_id, best = min(
        metrics.items(),
        key=lambda item: (-item[1].solve_rate, _time_of(item[1]), item[0]),
    )

    # 2) Targets not conclusively worse than the best (the best included).
    candidates = [best_id]
    for target_id in sorted(metrics):
        if target_id == best_id:
            continue
        comparison = comparisons.get((best_id, target_id))
        if comparison is None or not comparison.get("conclusive", False):
            candidates.append(target_id)

    # 3) Cheapest per verified solve inside the set — only when every member
    #    reports an effective cost. One unreported cost disables the economic
    #    recommendation entirely: never compare priced against unpriced.
    if any(metrics[tid].effective_cost_usd is None for tid in candidates):
        return Recommendation(
            best_quality_target=best_id,
            candidates_not_worse=candidates,
            recommended=None,
            reason=(
                f"{NO_COST_REASON}; best observed quality: {best_id} "
                f"({best.solve_rate * 100:.0f}% solve rate)."
            ),
        )

    recommended = min(
        candidates,
        key=lambda tid: (metrics[tid].effective_cost_usd, _time_of(metrics[tid]), tid),
    )
    return Recommendation(
        best_quality_target=best_id,
        candidates_not_worse=candidates,
        recommended=recommended,
        reason=(
            f"{recommended} has the lowest cost per verified solve among targets not "
            f"conclusively worse than {best_id} (best observed quality, "
            f"{best.solve_rate * 100:.0f}% solve rate)."
        ),
    )
