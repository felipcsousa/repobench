"""Statistical utilities: Wilson CI and paired bootstrap."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

from repobench.logging import get_logger

log = get_logger("analysis.statistics")

_Z_95 = 1.959963984540054  # z-value for 95% confidence


def wilson_ci(successes: int, total: int, z: float = _Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Returns (lower, upper) bounds. If total is 0, returns (0, 0).
    """
    if total <= 0:
        return 0.0, 0.0

    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denom

    return max(0.0, center - margin), min(1.0, center + margin)


def paired_bootstrap_difference(
    outcomes_a: Sequence[bool],
    outcomes_b: Sequence[bool],
    n_bootstrap: int = 10_000,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Paired bootstrap difference of pass rates.

    Performs a paired bootstrap (resampling task indices with replacement)
    with a deterministic seed.

    Returns (observed_difference_pp, ci_lower_pp, ci_upper_pp) where
    difference = pass_rate_a - pass_rate_b (in percentage points).
    """
    if len(outcomes_a) != len(outcomes_b):
        raise ValueError("Paired bootstrap requires equal-length outcome arrays")

    n = len(outcomes_a)
    if n == 0:
        return 0.0, 0.0, 0.0

    a = [1 if x else 0 for x in outcomes_a]
    b = [1 if x else 0 for x in outcomes_b]

    observed_diff = (sum(a) - sum(b)) / n * 100

    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(n_bootstrap):
        idx = [rng.randrange(n) for _ in range(n)]
        sum_a = sum(a[i] for i in idx)
        sum_b = sum(b[i] for i in idx)
        diffs.append((sum_a - sum_b) / n * 100)

    diffs.sort()
    lo_idx = max(0, int(0.025 * n_bootstrap) - 1)
    hi_idx = min(n_bootstrap - 1, int(0.975 * n_bootstrap) - 1)

    return observed_diff, diffs[lo_idx], diffs[hi_idx]
