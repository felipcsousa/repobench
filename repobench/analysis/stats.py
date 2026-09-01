"""Honest statistics: Wilson intervals and paired bootstrap (PRD §104-105)."""

from __future__ import annotations

import math
import random

from repobench.core.types import TrialOutcome, TrialResult


def wilson_ci(solved: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval on a proportion; bounds clamped to [0, 1].

    n == 0 carries no information -> (0.0, 0.0).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = solved / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def paired_bootstrap(
    trials_a: list[TrialResult],
    trials_b: list[TrialResult],
    *,
    n_boot: int = 10000,
    seed: int = 42,
) -> dict:
    """Paired bootstrap over the task intersection; diffs in percentage points.

    Constraint (PRD §105): `conclusive` is True only when the 95% percentile
    interval excludes 0 — a false winner is never declared. Seeded and
    deterministic for identical inputs.
    """
    by_a = {t.task_id: t for t in trials_a}
    by_b = {t.task_id: t for t in trials_b}
    common = sorted(set(by_a) & set(by_b))
    n_pairs = len(common)
    if n_pairs == 0:
        return {
            "n_pairs": 0,
            "mean_diff_pp": 0.0,
            "ci_lo_pp": 0.0,
            "ci_hi_pp": 0.0,
            "conclusive": False,
        }

    solved_a = [1.0 if by_a[task_id].outcome == TrialOutcome.SOLVED else 0.0 for task_id in common]
    solved_b = [1.0 if by_b[task_id].outcome == TrialOutcome.SOLVED else 0.0 for task_id in common]
    mean_diff_pp = (sum(solved_a) - sum(solved_b)) / n_pairs * 100.0

    rng = random.Random(seed)
    indices = range(n_pairs)
    diffs: list[float] = []
    for _ in range(n_boot):
        sample = rng.choices(indices, k=n_pairs)
        diff = (
            sum(solved_a[i] for i in sample) - sum(solved_b[i] for i in sample)
        ) / n_pairs * 100.0
        diffs.append(diff)
    diffs.sort()

    # Shared nearest-rank percentile (percentile in [0, 100]); the import is
    # local because metrics.py imports wilson_ci from this module.
    from repobench.analysis.metrics import nearest_rank

    ci_lo = nearest_rank(diffs, 2.5)
    ci_hi = nearest_rank(diffs, 97.5)
    conclusive = (ci_lo > 0 and ci_hi > 0) or (ci_lo < 0 and ci_hi < 0)
    return {
        "n_pairs": n_pairs,
        "mean_diff_pp": mean_diff_pp,
        "ci_lo_pp": ci_lo,
        "ci_hi_pp": ci_hi,
        "conclusive": conclusive,
    }
