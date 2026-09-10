"""Candidate funnel, scope profile and benchmarkability aggregations (PRD §30-32, §42-43).

Pure-model tests with hand-built candidates: every number is hand-computed, so
the honesty rules (None stays None, shares come from real counts) are asserted
directly.
"""

from __future__ import annotations

from repobench.config import ProjectConfig
from repobench.core.types import (
    Assessment,
    CandidateClass,
    RejectionCode,
    TaskStatus,
    TaskType,
)
from repobench.mining.benchmarkability import (
    LEGACY_MAX_IMPLEMENTATION_LOC,
    behavioral_loc_of,
    compute_benchmarkability,
    compute_candidate_funnel,
    compute_scope_profile,
)
from tests.fixtures.candidates import make_assessed_candidate


# -------------------------------------------------------------------- funnel


def test_funnel_stages_narrow_over_real_counts() -> None:
    candidates = [
        make_assessed_candidate(1),  # full evidence
        make_assessed_candidate(2, instruction="", confidence="C"),  # no instruction
        make_assessed_candidate(3, test_files=0),  # no verifier evidence
        make_assessed_candidate(4, implementation_loc=0),  # no implementation
    ]
    funnel = compute_candidate_funnel(candidates, merged_prs=9)
    assert funnel.merged_prs == 9
    assert funnel.human_changes == 4
    assert funnel.with_implementation == 3
    assert funnel.with_verifier_evidence == 2
    assert funnel.with_instruction == 1
    # legacy hard_caps mining assigns no classes — counts stay 0 (PRD §34)
    assert (funnel.high_confidence, funnel.medium_confidence, funnel.experimental) == (0, 0, 0)
    assert funnel.discovered == 4


def test_funnel_class_counts_come_from_assessment() -> None:
    candidates = [
        make_assessed_candidate(1, candidate_class=CandidateClass.HIGH_CONFIDENCE),
        make_assessed_candidate(2, candidate_class=CandidateClass.HIGH_CONFIDENCE),
        make_assessed_candidate(3, candidate_class=CandidateClass.MEDIUM_CONFIDENCE),
    ]
    funnel = compute_candidate_funnel(candidates, merged_prs=3)
    assert funnel.high_confidence == 2
    assert funnel.medium_confidence == 1
    assert funnel.experimental == 0


# ------------------------------------------------------------- scope profile


def test_scope_profile_percentiles_and_cap_pressure() -> None:
    # 4 otherwise-eligible PRs at LOC 100/200/500/900 — half exceed the legacy
    # 400 cap, so cap_pressure is exactly 50.
    candidates = [
        make_assessed_candidate(1, implementation_loc=100),
        make_assessed_candidate(2, implementation_loc=200),
        make_assessed_candidate(3, implementation_loc=500),
        make_assessed_candidate(4, implementation_loc=900),
    ]
    profile = compute_scope_profile(candidates)
    assert profile.n == 4
    assert profile.cap_pressure == 50.0
    assert LEGACY_MAX_IMPLEMENTATION_LOC == 400
    # nearest-rank percentiles over the sorted behavioral LOC [100,200,500,900]
    assert profile.p50 == 200
    assert profile.p90 == 900


def test_scope_profile_prefers_stored_behavioral_loc() -> None:
    # PRD §20: generated LOC discounted — behavioral 740, not raw 1700.
    candidates = [
        make_assessed_candidate(
            1, implementation_loc=1700, behavioral_loc=740, generated_loc=1200
        ),
    ]
    profile = compute_scope_profile(candidates)
    assert profile.p50 == 740


def test_scope_profile_empty_population_has_no_baseline() -> None:
    profile = compute_scope_profile([])
    assert profile.n == 0
    # None — "no baseline" must stay distinguishable from a measurement.
    assert profile.cap_pressure is None


def test_scope_profile_size_rejections_stay_in_population() -> None:
    # TASK_TOO_LARGE/TOO_SMALL/EXTREME are size-class codes (PRD §37): the PRs
    # whose fate the old cap decided stay in the cap-pressure population.
    candidates = [
        make_assessed_candidate(
            1, implementation_loc=900, rejection_code=RejectionCode.TASK_TOO_LARGE
        ),
        make_assessed_candidate(
            2,
            status=TaskStatus.FILTERED,
            rejection_code=RejectionCode.NO_INSTRUCTION,
        ),
    ]
    profile = compute_scope_profile(candidates)
    assert profile.n == 1


def test_behavioral_loc_falls_back_to_implementation_loc() -> None:
    assert behavioral_loc_of(Assessment(implementation_loc=120)) == 120
    assert (
        behavioral_loc_of(Assessment(implementation_loc=1700, behavioral_loc=740)) == 740
    )


# ----------------------------------------------------------- benchmarkability


def test_benchmarkability_components_hand_computed() -> None:
    candidates = [
        make_assessed_candidate(1, confidence="A"),
        make_assessed_candidate(2, confidence="C"),
        make_assessed_candidate(3, instruction="", confidence="C"),
        make_assessed_candidate(4, test_files=0, instruction="", confidence="C"),
        make_assessed_candidate(
            5,
            status=TaskStatus.FILTERED,
            rejection_code=RejectionCode.HISTORY_UNSUPPORTED,
            instruction="",
            confidence="C",
        ),
    ]
    report = compute_benchmarkability(candidates, None)
    # verifier: 4 of 5 carry a test file → 80
    assert report.components["verifier_evidence"] == 80
    # instructions: A=1.0 + C=0.5 + 3×none over 5 → 30
    assert report.components["instructions"] == 30
    # history: 4 of 5 not history-rejected → 80
    assert report.components["history"] == 80
    # environment: project_cfg None → neutral 50, never invented
    assert report.components["environment"] == 50


def test_benchmarkability_bands_and_diversity() -> None:
    # two distinct task types over 2 non-filtered candidates → max entropy → 100
    two_types = [
        make_assessed_candidate(1, task_type=TaskType.BUGFIX),
        make_assessed_candidate(2, task_type=TaskType.FEATURE),
    ]
    report = compute_benchmarkability(two_types, None)
    assert report.components["candidate_diversity"] == 100
    assert report.band in ("MEDIUM", "HIGH")

    one_type = [
        make_assessed_candidate(1, task_type=TaskType.BUGFIX),
        make_assessed_candidate(2, task_type=TaskType.BUGFIX),
    ]
    single = compute_benchmarkability(one_type, None)
    assert single.components["candidate_diversity"] == 0


def test_benchmarkability_band_boundaries() -> None:
    report = compute_benchmarkability([], ProjectConfig())
    # empty repo: every share component 0, environment 100 (all known? None cfg
    # would be 50 — with a cfg, assess_environment on an empty ProjectConfig
    # scores 0.0 known signals → 0). Diversidade 0 → all components 0 → LOW.
    assert report.band == "LOW"
    assert report.score == 0


def test_benchmarkability_notes_flag_weak_test_evidence() -> None:
    # integration is 60% of the universe but none of its candidates has test
    # evidence — the exact PRD §59 "weak test evidence" risk line.
    candidates = [
        make_assessed_candidate(1, task_type=TaskType.BUGFIX, test_files=1),
        make_assessed_candidate(2, task_type=TaskType.BUGFIX, test_files=1),
        make_assessed_candidate(3, task_type=TaskType.INTEGRATION, test_files=0),
        make_assessed_candidate(4, task_type=TaskType.INTEGRATION, test_files=0),
        make_assessed_candidate(5, task_type=TaskType.INTEGRATION, test_files=0),
    ]
    report = compute_benchmarkability(candidates, None)
    assert any("integration" in note for note in report.notes)
