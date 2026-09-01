"""Complexity buckets for mined tasks (PRD §69).

Relative repository complexity from implementation LOC, implementation files and
packages touched. Grand tasks may be excluded from the V1 benchmark automatically.
"""

from __future__ import annotations

from repobench.config import TaskMiningConfig
from repobench.core.types import Complexity


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
