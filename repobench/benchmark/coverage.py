"""Coverage calculation: TVD converted to coverage percentages."""

from __future__ import annotations

from collections import Counter


def calculate_coverage(
    benchmark_dist: dict[str, dict[str, float]],
    workload_dist: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Calculate coverage per dimension.

    Args:
        benchmark_dist: {dimension: {category: probability}}
        workload_dist:  {dimension: {category: probability}}

    Returns:
        {dimension: coverage_percentage} where coverage = 100 * (1 - TVD).
    """
    coverage: dict[str, float] = {}

    all_dims = set(benchmark_dist.keys()) | set(workload_dist.keys())
    for dim in all_dims:
        b = benchmark_dist.get(dim, {})
        w = workload_dist.get(dim, {})
        tvd = _tvd_from_dicts(b, w)
        coverage[dim] = round(100 * (1 - tvd), 1)

    return coverage


def distributions_from_counts(
    benchmark_counts: dict[str, Counter],
    workload_counts: dict[str, Counter],
) -> dict[str, float]:
    """Convenience: compute coverage from Counter objects."""
    b = {
        dim: {k: c / sum(c.values()) if sum(c.values()) else 0.0 for k, c in counts.items()}
        for dim, counts in benchmark_counts.items()
    }
    w = {
        dim: {k: c / sum(c.values()) if sum(c.values()) else 0.0 for k, c in counts.items()}
        for dim, counts in workload_counts.items()
    }
    return calculate_coverage(b, w)


def _tvd_from_dicts(bench: dict[str, float], workload: dict[str, float]) -> float:
    """Total Variation Distance between two probability dicts."""
    keys = set(bench.keys()) | set(workload.keys())
    if not keys:
        return 0.0
    return 0.5 * sum(abs(bench.get(k, 0.0) - workload.get(k, 0.0)) for k in keys)
