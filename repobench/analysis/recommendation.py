"""Cost-aware recommendation logic."""

from __future__ import annotations

from repobench.logging import get_logger
from repobench.models import ConfigMetrics

log = get_logger("analysis.recommendation")


def recommend(
    metrics_dict: dict[str, ConfigMetrics],
) -> tuple[str | None, str]:
    """Recommend a default agent configuration.

    Policy (``cost_effective``):
    1. Find the configuration with the highest observed pass rate.
    2. Identify configurations whose quality difference is not
       statistically conclusive (overlapping Wilson CIs).
    3. Within that set, choose the lowest cost per verified solve.

    Returns (config_name, reason).
    """
    if not metrics_dict:
        return None, "No configurations with results available."

    # Consider only configs with trials
    with_results = {name: m for name, m in metrics_dict.items() if m.total > 0}
    if not with_results:
        return None, "No configurations have completed trials."

    # 1. Best observed pass rate
    best_name = max(with_results, key=lambda n: with_results[n].pass_rate)
    best = with_results[best_name]

    # 2. Statistically indistinguishable set (overlapping Wilson CIs)
    indistinguishable = []
    for name, m in with_results.items():
        if _cis_overlap(best, m):
            indistinguishable.append(name)

    # 3. Lowest cost per verified solve among indistinguishable
    with_cost = [
        (name, with_results[name])
        for name in indistinguishable
        if with_results[name].cost_per_solve is not None
    ]

    if with_cost:
        recommended = min(with_cost, key=lambda nm: nm[1].cost_per_solve)[0]
        reason = (
            "lowest cost per verified solve among configurations "
            "statistically indistinguishable from observed best quality."
        )
    else:
        # No cost data: pick best observed pass rate
        recommended = best_name
        reason = "best observed pass rate (no cost data available)."

    log.info("Recommendation: %s (%s)", recommended, reason)
    return recommended, reason


def pareto_frontier(
    metrics_dict: dict[str, ConfigMetrics],
) -> list[str]:
    """Identify configurations on the Pareto frontier.

    A configuration is dominated if another config has >= pass rate AND
    <= cost per solve (with at least one strict inequality).

    Returns config names on the frontier (highest quality first).
    """
    with_cost = {
        name: m for name, m in metrics_dict.items() if m.total > 0 and m.cost_per_solve is not None
    }
    if not with_cost:
        return list(metrics_dict.keys())

    frontier: list[str] = []
    for name, m in with_cost.items():
        dominated = False
        for other_name, other in with_cost.items():
            if other_name == name:
                continue
            if (
                other.pass_rate >= m.pass_rate
                and other.cost_per_solve <= m.cost_per_solve
                and (other.pass_rate > m.pass_rate or other.cost_per_solve < m.cost_per_solve)
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(name)

    frontier.sort(key=lambda n: with_cost[n].pass_rate, reverse=True)
    return frontier


def _cis_overlap(a: ConfigMetrics, b: ConfigMetrics) -> bool:
    """Check if two Wilson CIs overlap (statistically indistinguishable)."""
    return not (a.ci_upper < b.ci_lower or b.ci_upper < a.ci_lower)
