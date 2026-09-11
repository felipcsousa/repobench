"""Shadow mode: adaptive mining evaluated alongside hard_caps (PRD §77 Phase B).

`size_policy: shadow` runs BOTH decisions on the same window — hard_caps keeps
controlling what is persisted and built, while the adaptive outcome is reduced
to a delta and reported. The recorded disagreement is the calibration data for
flipping the default later (PRD §52: old vs new on the same repository); no
shadow candidate ever reaches storage or a benchmark.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from repobench.core.types import (
    SIZE_CLASS_REJECTIONS,
    CandidateInfo,
    TaskStatus,
)


class ShadowDelta(BaseModel):
    """What adaptive mining WOULD decide while hard_caps still controls.

    Every count is a real comparison of the two passes over the same window —
    never an extrapolation. `additional_candidates` are PRs adaptive surfaces
    that hard_caps filtered; `recovered_from_size_caps` is the subset whose
    hard_caps rejection was purely a size decision."""

    controlling_policy: Literal["hard_caps", "adaptive"] = "hard_caps"
    hard_caps_pool: int = 0
    adaptive_pool: int = 0
    additional_candidates: int = 0
    recovered_from_size_caps: int = 0
    by_complexity: dict[str, int] = Field(default_factory=dict)
    adaptive_class_counts: dict[str, int] = Field(default_factory=dict)


def _pool(candidates: list[CandidateInfo]) -> dict[int, CandidateInfo]:
    """The validation-eligible candidates by PR number: DISCOVERED for
    hard_caps, DISCOVERED + LOW_PRIORITY for adaptive (PRD §37)."""
    return {
        c.pr.number: c
        for c in candidates
        if c.status in (TaskStatus.DISCOVERED, TaskStatus.LOW_PRIORITY)
    }


def compute_shadow_delta(
    hard_caps: list[CandidateInfo], adaptive: list[CandidateInfo]
) -> ShadowDelta:
    """Reduce the two mining passes to the decision disagreement (PRD §77)."""
    hard_pool = _pool(hard_caps)
    adaptive_pool = _pool(adaptive)
    by_number_hard = {c.pr.number: c for c in hard_caps}

    additional = sorted(set(adaptive_pool) - set(hard_pool))
    # Size-class rejections are the decisions adaptive mining exists to revisit
    # (PRD §10): TASK_TOO_LARGE/TASK_TOO_SMALL say "too big/small", not
    # "invalid" (SIZE_CLASS_REJECTIONS in repobench.core.types).
    recovered = [
        number
        for number in additional
        if by_number_hard.get(number) is not None
        and by_number_hard[number].rejection_code in SIZE_CLASS_REJECTIONS
    ]
    by_complexity: dict[str, int] = {}
    for number in additional:
        complexity = adaptive_pool[number].assessment.complexity
        key = complexity.value
        by_complexity[key] = by_complexity.get(key, 0) + 1
    class_counts: dict[str, int] = {}
    for candidate in adaptive_pool.values():
        if candidate.assessment.candidate_class is None:
            continue
        key = candidate.assessment.candidate_class.value
        class_counts[key] = class_counts.get(key, 0) + 1
    # Deterministic presentation regardless of PR iteration order.
    by_complexity = dict(sorted(by_complexity.items()))
    class_counts = dict(sorted(class_counts.items()))

    return ShadowDelta(
        controlling_policy="hard_caps",
        hard_caps_pool=len(hard_pool),
        adaptive_pool=len(adaptive_pool),
        additional_candidates=len(additional),
        recovered_from_size_caps=len(recovered),
        by_complexity=by_complexity,
        adaptive_class_counts=class_counts,
    )
