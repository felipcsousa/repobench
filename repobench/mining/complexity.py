"""Complexity buckets for mined tasks (PRD §69).

Relative repository complexity from implementation LOC, implementation files and
packages touched. Grand tasks may be excluded from the V1 benchmark automatically.
"""

from __future__ import annotations

from repobench.config import TaskMiningConfig
from repobench.core.types import Complexity
from repobench.mining.scope import complexity_from_percentile


def compute_complexity(
    implementation_loc: int,
    implementation_files: int,
    packages_touched: int,
    cfg: TaskMiningConfig,
) -> Complexity:
    # Any single strong signal makes the task large; otherwise both a small diff
    # and very few files make it small.
    if (
        implementation_loc >= cfg.large_loc_min
        or implementation_files >= cfg.large_files_min
        or packages_touched >= 3
    ):
        return Complexity.LARGE
    if implementation_loc <= cfg.small_loc_max and implementation_files <= 2:
        return Complexity.SMALL
    return Complexity.MEDIUM


def compute_complexity_percentile(percentile: float) -> Complexity:
    """Repository-relative complexity buckets (PRD §19): small = P0-P35,
    medium = P35-P80, large = P80-P95, xlarge = P95+.

    Thin alias of `repobench.mining.scope.complexity_from_percentile` — one
    implementation, exposed here so complexity callers have a single import
    site alongside `compute_complexity`."""
    return complexity_from_percentile(percentile)
