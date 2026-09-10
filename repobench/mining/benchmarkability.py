"""Candidate funnel, scope profile and repository benchmarkability (PRD §30-32, §42-43, §59).

Pure, deterministic aggregations over mined candidates: no I/O, no randomness
(PRD §72). The CLI service layer feeds these models to the renderers and to the
local telemetry artifact — every number is derived from real candidate counts,
never invented (PRD §53-54): a missing measurement stays None / 0 with an
explicit label, it is never papered over with a plausible-looking figure.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from repobench.config import ProjectConfig
from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    RejectionCode,
    TaskStatus,
)
from repobench.mining.assessment import assess_environment
from repobench.mining.scope import distribution_stats

# The historical hard-cap default (PRD §32 "Previous default cap 400"): the
# max_implementation_loc every repository was filtered by before adaptive
# mining. Kept literal so the cap-pressure diagnostic always measures against
# the same historical baseline, whatever the user's current config says.
LEGACY_MAX_IMPLEMENTATION_LOC = 400

Band = Literal["HIGH", "MEDIUM", "LOW"]


class CandidateFunnel(BaseModel):
    """The candidate funnel of one analyze run (PRD §30).

    The stage rows narrow monotonically over the mined universe:
    with_verifier_evidence counts candidates that have BOTH implementation and
    test evidence; with_instruction counts those that additionally carry a
    usable instruction. Class counts (high/medium/experimental) come from the
    adaptive assessment — legacy hard_caps mining never assigns classes, so its
    counts stay 0 and renderers must show that absence explicitly (PRD §34).
    """

    merged_prs: int = 0
    human_changes: int = 0
    with_implementation: int = 0
    with_verifier_evidence: int = 0
    with_instruction: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    experimental: int = 0
    discovered: int = 0
    low_priority: int = 0


class ScopeProfile(BaseModel):
    """Repository change-size profile (PRD §32).

    Percentiles are behavioral LOC (PRD §20: generated code discounted) over
    "otherwise eligible" PRs — candidates that cleared the methodological gates.
    `cap_pressure` is the share (in %) of those PRs whose implementation LOC
    exceeds the old 400-LOC default cap. `cap_pressure` is None exactly when
    n == 0: with no eligible PR there is no baseline, and "no baseline" must
    stay distinguishable from a measurement.
    """

    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    n: int = 0
    cap_pressure: float | None = None


class BenchmarkabilityReport(BaseModel):
    """Repository benchmarkability before any benchmark build (PRD §42-43).

    Five 0-100 components and their integer average. `notes` carries 1-3 honest
    derived statements — each one is computed from real counts, never invented.
    """

    score: int
    band: Band
    components: dict[str, int]
    notes: list[str] = Field(default_factory=list)


# Rejections that stand for "would be eligible except for its size" (PRD §37):
# they stay inside the cap-pressure population because they are exactly the PRs
# whose fate the old cap decided. Methodological gates (history, instruction,
# verifier evidence, implementation exists) remove a PR from the population.
_SIZE_REJECTIONS = frozenset(
    {
        RejectionCode.TASK_TOO_SMALL,
        RejectionCode.TASK_TOO_LARGE,
        RejectionCode.TASK_EXTREME_SCOPE,
    }
)

# Instruction provenance weights (PRD §14): tiers A/B are strong evidence,
# tier C is possibly solution-contaminated (half weight), tier D is derived
# from the solution by construction (lighter still).
_INSTRUCTION_WEIGHTS: dict[str, float] = {"A": 1.0, "B": 1.0, "C": 0.5, "D": 0.3}


def behavioral_loc_of(assessment: Assessment) -> int:
    """The assessment's behavioral LOC (PRD §20).

    Adaptive assessments store the generated-discounted figure; legacy
    hard_caps assessments never recorded generated LOC, so their raw
    implementation LOC is the honest behavioral figure — nothing is discounted
    that we cannot see.
    """
    if assessment.behavioral_loc is not None:
        return assessment.behavioral_loc
    return assessment.implementation_loc


def compute_candidate_funnel(candidates: list[CandidateInfo], merged_prs: int) -> CandidateFunnel:
    """Funnel counts over the mined universe (PRD §30): every stage is a real
    count, `merged_prs` (the window's merged PRs, bots included) anchors the top."""
    with_implementation = [c for c in candidates if c.assessment.implementation_loc > 0]
    with_verifier = [c for c in with_implementation if c.assessment.test_files >= 1]
    with_instruction = [c for c in with_verifier if c.assessment.instruction]
    return CandidateFunnel(
        merged_prs=merged_prs,
        human_changes=len(candidates),
        with_implementation=len(with_implementation),
        with_verifier_evidence=len(with_verifier),
        with_instruction=len(with_instruction),
        high_confidence=sum(
            1
            for c in candidates
            if c.assessment.candidate_class is CandidateClass.HIGH_CONFIDENCE
        ),
        medium_confidence=sum(
            1
            for c in candidates
            if c.assessment.candidate_class is CandidateClass.MEDIUM_CONFIDENCE
        ),
        experimental=sum(
            1
            for c in candidates
            if c.assessment.candidate_class is CandidateClass.EXPERIMENTAL
        ),
        discovered=sum(1 for c in candidates if c.status is TaskStatus.DISCOVERED),
        low_priority=sum(1 for c in candidates if c.status is TaskStatus.LOW_PRIORITY),
    )


def compute_scope_profile(candidates: list[CandidateInfo]) -> ScopeProfile:
    """Repository change profile and cap pressure (PRD §32).

    The population is every candidate that cleared the methodological gates —
    `rejection_code` None or a size-class code (see _SIZE_REJECTIONS). Over it:
    behavioral-LOC P50/P75/P90 (nearest-rank, repobench.mining.scope) and the
    share of implementation LOCs above the old 400-LOC default cap. An empty
    population yields n == 0 and cap_pressure None — no baseline, never a zero
    that would read as a measurement.
    """
    eligible = [
        c
        for c in candidates
        if c.rejection_code is None or c.rejection_code in _SIZE_REJECTIONS
    ]
    stats = distribution_stats([float(behavioral_loc_of(c.assessment)) for c in eligible])
    cap_pressure = None
    if eligible:
        above = sum(
            1
            for c in eligible
            if c.assessment.implementation_loc > LEGACY_MAX_IMPLEMENTATION_LOC
        )
        cap_pressure = round(100.0 * above / len(eligible), 1)
    return ScopeProfile(
        p50=stats["p50"],
        p75=stats["p75"],
        p90=stats["p90"],
        n=len(eligible),
        cap_pressure=cap_pressure,
    )


def _diversity_score(pool: list[CandidateInfo]) -> int:
    """Normalized entropy of the task-type distribution over `pool` (PRD §42),
    ×100. Natural log over the present types; k = 1 or an empty pool score 0
    (all work in one type is zero diversity, a real measurement, not absence)."""
    if not pool:
        return 0
    counts: dict[str, int] = {}
    for candidate in pool:
        key = candidate.assessment.task_type.value
        counts[key] = counts.get(key, 0) + 1
    k = len(counts)
    if k <= 1:
        return 0
    total = len(pool)
    entropy = -sum((count / total) * math.log(count / total) for count in counts.values())
    return round(100.0 * entropy / math.log(k))


def _benchmarkability_notes(
    candidates: list[CandidateInfo], pool: list[CandidateInfo], diversity: int
) -> list[str]:
    """1-3 honest derived statements (PRD §42): each note fires from a real
    count threshold — a major work type with weak test evidence, a thin
    candidate pool, a workload concentrated in one type. Nothing else."""
    notes: list[str] = []
    if 0 < len(pool) < 10:
        notes.append("small candidate pool")
    total = len(candidates)
    by_type: dict[str, list[CandidateInfo]] = {}
    for candidate in candidates:
        by_type.setdefault(candidate.assessment.task_type.value, []).append(candidate)
    for task_type in sorted(by_type):
        group = by_type[task_type]
        if total == 0 or len(group) / total < 0.10:
            continue  # below 10% universe share the type is not a major pattern
        evidence_rate = sum(1 for c in group if c.assessment.test_files >= 1) / len(group)
        if evidence_rate < 0.30:
            notes.append(f"{task_type} work has weak test evidence")
    if pool and diversity < 25:
        notes.append("candidate diversity is low — the workload concentrates in few task types")
    return notes[:3]


def compute_benchmarkability(
    candidates: list[CandidateInfo], project_cfg: ProjectConfig | None
) -> BenchmarkabilityReport:
    """Repository benchmarkability score (PRD §42-43): five 0-100 components,
    integer average, HIGH >= 75 / MEDIUM >= 45 / LOW below.

    Component notes:
    - verifier_evidence: share of ALL mined candidates carrying at least one
      test file (PRD §15 evidence, PRD §9 gate).
    - instructions: provenance-weighted availability (PRD §14): A/B count 1.0,
      C 0.5, D 0.3, no instruction 0, normalized by the candidate count.
    - history: share of candidates NOT rejected HISTORY_UNSUPPORTED (PRD §9).
    - environment: repo-level, assessed ONCE from the project config (PRD §23) —
      it does not vary per candidate, so averaging over candidates would invent
      variance. project_cfg None scores the neutral midpoint 50
      (assessment.environment_unknown) instead of pretending detection ran.
    - candidate_diversity: normalized entropy of task types over non-filtered
      candidates (see _diversity_score).
    """
    denom = max(len(candidates), 1)

    verifier_evidence = round(
        100.0 * sum(1 for c in candidates if c.assessment.test_files >= 1) / denom
    )
    instruction_weight = sum(
        _INSTRUCTION_WEIGHTS.get(c.assessment.instruction_confidence, 0.0)
        for c in candidates
        if c.assessment.instruction
    )
    instructions = round(100.0 * instruction_weight / denom)
    history = round(
        100.0
        * sum(
            1 for c in candidates if c.rejection_code is not RejectionCode.HISTORY_UNSUPPORTED
        )
        / denom
    )
    environment = (
        round(assess_environment(project_cfg).score * 100)
        if project_cfg is not None
        else 50
    )
    pool = [c for c in candidates if c.status is not TaskStatus.FILTERED]
    diversity = _diversity_score(pool)

    components = {
        "verifier_evidence": verifier_evidence,
        "instructions": instructions,
        "history": history,
        "environment": environment,
        "candidate_diversity": diversity,
    }
    score = round(sum(components.values()) / len(components))
    band: Band = "HIGH" if score >= 75 else "MEDIUM" if score >= 45 else "LOW"
    return BenchmarkabilityReport(
        score=score,
        band=band,
        components=components,
        notes=_benchmarkability_notes(candidates, pool, diversity),
    )
