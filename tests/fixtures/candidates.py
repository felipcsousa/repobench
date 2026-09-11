"""Shared hand-built `CandidateInfo` fixtures for the adaptive-mining tests.

Single home for the scored-candidate builder previously forked across
test_priority.py, test_benchmarkability.py, test_shadow.py and test_scope.py.
Import directly (`from tests.fixtures.candidates import make_assessed_candidate`)
— conftest.py puts this package on sys.path like tests.fixtures.gitutil.

The builder serves the adaptive test shapes: a candidate carrying the mining
assessment (PRD §11-24) with the adaptive-v1 fields (PRD §68 — candidate score
and class, scope percentile, behavioral/generated LOC, risk flags, assessment
version) settable per test and defaulting to "not computed" (None), exactly as
legacy hard_caps mining leaves them.
"""

from __future__ import annotations

from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    InstructionConfidence,
    PRInfo,
    RejectionCode,
    TaskStatus,
    TaskType,
)


def make_assessed_candidate(
    number: int,
    *,
    task_type: TaskType = TaskType.BUGFIX,
    subsystem: str = "math",
    status: TaskStatus = TaskStatus.DISCOVERED,
    rejection_code: RejectionCode | None = None,
    score: float | None = None,
    candidate_class: CandidateClass | None = None,
    instruction: str = "fix the thing",
    confidence: InstructionConfidence = "B",
    test_files: int = 1,
    test_loc: int = 20,
    implementation_loc: int = 40,
    implementation_files: int = 2,
    behavioral_loc: int | None = None,
    generated_loc: int | None = None,
    scope_percentile: float | None = None,
    risk_flags: list[str] | None = None,
    assessment_version: str | None = None,
    with_shas: bool = True,
    candidate_id: str | None = None,
) -> CandidateInfo:
    """A scored adaptive-shaped candidate — hand-built, no repository behind it.

    Defaults describe a small valid bugfix so a bare call is pool-eligible;
    every adaptive-v1 field (PRD §68) defaults to None = not computed.

    Knobs: the assessment fields above plus `status`/`rejection_code` for
    funnel and shadow-delta tests. `with_shas=False` yields a bare
    `PRInfo(number=number)` for tests that never derive task ids (storage
    round-trips). `candidate_id` overrides the default `c{number:04d}` (the
    storage tests key rows by explicit ids). With SHAs, base/head/merge follow
    the `b/h/m{number:04d}` + 32-zeros scheme so `task_id_for` derivations —
    which hash pr.base_sha/merge_sha — stay stable across runs.
    """
    return CandidateInfo(
        candidate_id=candidate_id if candidate_id is not None else f"c{number:04d}",
        pr=PRInfo(
            number=number,
            base_sha=(f"b{number:04d}" + "0" * 32) if with_shas else None,
            head_sha=(f"h{number:04d}" + "0" * 32) if with_shas else None,
            merge_sha=(f"m{number:04d}" + "0" * 32) if with_shas else None,
        ),
        status=status,
        rejection_code=rejection_code,
        assessment=Assessment(
            task_type=task_type,
            subsystem=subsystem,
            candidate_score=score,
            candidate_class=candidate_class,
            instruction=instruction,
            instruction_confidence=confidence,
            test_files=test_files,
            test_loc=test_loc,
            implementation_loc=implementation_loc,
            implementation_files=implementation_files,
            behavioral_loc=behavioral_loc,
            generated_loc=generated_loc,
            scope_percentile=scope_percentile,
            risk_flags=[] if risk_flags is None else risk_flags,
            assessment_version=assessment_version,
        ),
    )
