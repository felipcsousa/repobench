"""Configuration metrics aggregation from trials."""

from __future__ import annotations

import statistics
from collections.abc import Iterable

from repobench.analysis.statistics import wilson_ci
from repobench.logging import get_logger
from repobench.models import ConfigMetrics, Trial

log = get_logger("analysis.metrics")


def compute_config_metrics(trials: Iterable[Trial]) -> ConfigMetrics:
    """Compute aggregated metrics for one agent configuration."""
    trials = list(trials)
    metrics = ConfigMetrics()

    if not trials:
        return metrics

    solved = [t for t in trials if t.solved]
    metrics.solved = len(solved)
    metrics.total = len(trials)
    metrics.pass_rate = len(solved) / len(trials)

    # Wilson 95% CI
    ci_lower, ci_upper = wilson_ci(len(solved), len(trials))
    metrics.ci_lower = ci_lower
    metrics.ci_upper = ci_upper

    # Economics
    costs = [t.cost_usd for t in trials if t.cost_usd is not None]
    if costs:
        metrics.total_cost = sum(costs)
        metrics.mean_cost_task = metrics.total_cost / len(costs)
        if metrics.solved > 0:
            metrics.cost_per_solve = metrics.total_cost / metrics.solved

    # Efficiency
    prompt_tokens = [t.prompt_tokens for t in trials if t.prompt_tokens is not None]
    completion_tokens = [t.completion_tokens for t in trials if t.completion_tokens is not None]
    metrics.total_prompt_tokens = sum(prompt_tokens)
    metrics.total_completion_tokens = sum(completion_tokens)
    if metrics.solved > 0:
        total_tokens = metrics.total_prompt_tokens + metrics.total_completion_tokens
        metrics.tokens_per_solve = round(total_tokens / metrics.solved)

    # Performance
    durations = [t.duration_ms for t in trials if t.duration_ms is not None]
    if durations:
        metrics.p50_duration_ms = round(statistics.median(durations))
        sorted_d = sorted(durations)
        p90_idx = min(len(sorted_d) - 1, round(0.9 * (len(sorted_d) - 1)))
        metrics.p90_duration_ms = sorted_d[p90_idx]

    return metrics
