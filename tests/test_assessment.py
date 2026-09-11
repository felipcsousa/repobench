"""Candidate assessment tests (PRD v0.9, §11-24, §71-72): the deterministic
component scorers, the weighted aggregate, the candidate classes, and the risk
flags — every score must be explainable and recomputable from its evidence."""

from __future__ import annotations

import pytest

from repobench.config import CandidateScoreConfig, ProjectConfig, TaskMiningConfig
from repobench.core.types import CandidateClass
from repobench.mining.assessment import (
    ASSESSMENT_VERSION,
    CANDIDATE_SCORE_WEIGHTS,
    ComponentScore,
    aggregate,
    assess_environment,
    assess_instruction,
    assess_isolation,
    assess_reconstructability,
    assess_scope,
    assess_verifier,
    classify,
    environment_unknown,
    risk_flags,
)
from repobench.mining.instruction import InstructionResult
from repobench.mining.scope import ScopeFeatures


def _instruction(confidence: str, source: str = "pr_body") -> InstructionResult:
    return InstructionResult(text="Fix charge rounding", source=source, confidence=confidence)  # type: ignore[arg-type]


def _features(**overrides) -> ScopeFeatures:
    values: dict = dict(
        implementation_loc=100,
        test_loc=50,
        generated_loc=0,
        implementation_files=2,
        test_files=2,
        dirs_touched=1,
        packages_touched=1,
        languages=["python"],
        impl_paths=["src/a.py"],
    )
    values.update(overrides)
    return ScopeFeatures(**values)


# ---------------------------------------------------------------- instruction


def test_instruction_tier_mapping() -> None:
    # PRD §14: tier scores; None never occurs past the NO_INSTRUCTION gate but
    # must still score 0.0 honestly.
    assert assess_instruction(_instruction("A", "issue")).score == 0.95
    assert assess_instruction(_instruction("B")).score == 0.75
    assert assess_instruction(_instruction("C")).score == 0.45
    none_score = assess_instruction(None)
    assert none_score.score == 0.0
    assert none_score.evidence == ["no instruction"]


def test_instruction_evidence_names_the_tier() -> None:
    assert assess_instruction(_instruction("A", "issue")).evidence == [
        "linked issue predates PR (tier A)"
    ]
    assert assess_instruction(_instruction("B")).evidence == ["PR body problem statement (tier B)"]
    # Tier C differs by source: title vs contaminated body (PRD §14).
    assert assess_instruction(_instruction("C", "title")).evidence == [
        "title-derived, possibly solution-contaminated (tier C)"
    ]
    assert assess_instruction(_instruction("C", "pr_body")).evidence == [
        "PR body flagged solution-contaminated (tier C)"
    ]


# ---------------------------------------------------------------- verifier


def test_verifier_base_without_test_signals() -> None:
    features = _features(test_files=0, test_loc=0, implementation_loc=100)
    score = assess_verifier(features)
    assert score.score == 0.40
    assert score.evidence == []  # nothing fired — no invented evidence


def test_verifier_evidence_example() -> None:
    # PRD §36 inspect example: 3 test files, 74 test LOC, ratio 0.42.
    features = _features(
        test_files=3,
        test_loc=74,
        implementation_loc=176,
        impl_paths=["src/a.py", "src/b.py", "src/c.py"],
    )
    score = assess_verifier(features)
    assert score.score == 0.90  # 0.40 + files + LOC + ratio (74/176 = 0.42 >= 0.3)
    assert score.evidence == ["3 test files", "74 test LOC", "test/impl ratio 0.42"]


def test_verifier_monotonic_in_test_signals_and_capped() -> None:
    ladder = [
        _features(test_files=0, test_loc=0),
        _features(test_files=1, test_loc=5),
        _features(test_files=2, test_loc=5),
        _features(test_files=2, test_loc=45),
        _features(test_files=3, test_loc=160, implementation_loc=100),
    ]
    scores = [assess_verifier(features).score for features in ladder]
    assert scores == sorted(scores)
    # Every bonus fires (0.40+0.20+0.20+0.20+0.10 = 1.10) — capped at 1.0.
    assert scores[-1] == 1.0


# ---------------------------------------------------------------- reconstructability


def test_reconstructability_resolvable_history() -> None:
    score = assess_reconstructability("b", "h", "m", resolvable=True)
    assert score.score == 1.0
    assert score.evidence == ["clean history: base/head/merge SHAs resolvable"]


def test_reconstructability_missing_merge_sha() -> None:
    score = assess_reconstructability("b", "h", None, resolvable=False)
    assert score.score == 0.30
    assert score.evidence == ["merge SHA missing"]


def test_reconstructability_unresolvable() -> None:
    score = assess_reconstructability("b", "h", "m", resolvable=False)
    assert score.score == 0.0


# ---------------------------------------------------------------- scope


def test_scope_confidence_from_percentile() -> None:
    # PRD §36: P82 -> 0.18.
    assert assess_scope(82.0).score == 0.18
    assert assess_scope(0.0).score == 1.0
    assert assess_scope(100.0).score == 0.0
    assert assess_scope(83.3).score == 0.17


def test_scope_neutral_without_baseline() -> None:
    score = assess_scope(None)
    assert score.score == 0.50
    assert score.evidence == ["no repository baseline (n too small)"]


# ---------------------------------------------------------------- isolation


def test_isolation_coherent_change() -> None:
    score = assess_isolation(_features(dirs_touched=1, packages_touched=1), subsystem="src")
    assert score.score == 0.90  # 0.60 + single package + coherent paths
    assert score.evidence == ["single package", "coherent paths", "subsystem src"]


def test_isolation_penalized_scatter() -> None:
    features = _features(
        dirs_touched=3,
        packages_touched=3,
        languages=["python", "typescript"],
    )
    score = assess_isolation(features, subsystem="unknown")
    assert score.score == 0.20  # 0.60 - multi-package - cross-language
    assert score.evidence == ["multi-package change", "cross-language implementation"]


def test_isolation_unremarkable_change_stays_at_base() -> None:
    # Two packages (neither bonus nor penalty), many dirs (no coherence bonus).
    score = assess_isolation(_features(dirs_touched=4, packages_touched=2), subsystem="unknown")
    assert score.score == 0.60
    assert score.evidence == []


# ---------------------------------------------------------------- environment


FULL_PROJECT = ProjectConfig(
    language="python",
    package_manager="uv",
    install_command="uv sync --frozen",
    test_command="python -m pytest",
    regression_command="python -m pytest",
)


def test_environment_all_signals_known() -> None:
    score = assess_environment(FULL_PROJECT)
    assert score.score == 1.0  # 0.10+0.20+0.20+0.30+0.20
    assert score.evidence == [
        "language python",
        "package manager uv",
        "install command known",
        "test command known (pytest)",
        "regression command known",
    ]


def test_environment_partial_signals() -> None:
    score = assess_environment(ProjectConfig(language="python", package_manager="pip"))
    assert score.score == 0.30
    assert score.evidence == ["language python", "package manager pip"]


def test_environment_nothing_known_scores_zero() -> None:
    # Honesty rule (PRD §53-54): an empty config earns nothing it did not detect.
    score = assess_environment(ProjectConfig())
    assert score.score == 0.0
    assert score.evidence == []


def test_environment_unknown_is_neutral() -> None:
    score = environment_unknown()
    assert score.score == 0.50
    assert score.evidence == ["project environment unknown"]


# ---------------------------------------------------------------- aggregate


def test_aggregate_exact_weights() -> None:
    # All components perfect except scope at P82 (0.18):
    # 0.25+0.25+0.20+0.10+0.10+0.018 = 0.918 -> 0.92.
    one = ComponentScore(score=1.0, evidence=[])
    total = aggregate(
        {
            "instruction": one,
            "verifier": one,
            "reconstructability": one,
            "isolation": one,
            "environment": one,
            "scope": ComponentScore(score=0.18, evidence=[]),
        }
    )
    assert total == 0.92


def test_aggregate_prd_example_ballpark() -> None:
    # PRD §11's worked example: instruction 0.92, verifier 0.86,
    # reconstructability 0.95, scope 0.48, isolation 0.81, environment 0.73.
    # Hand-computed: 0.837 -> 0.84 (the PRD's 0.81 is a rounded illustration).
    components = {
        "instruction": ComponentScore(score=0.92, evidence=[]),
        "verifier": ComponentScore(score=0.86, evidence=[]),
        "reconstructability": ComponentScore(score=0.95, evidence=[]),
        "isolation": ComponentScore(score=0.81, evidence=[]),
        "environment": ComponentScore(score=0.73, evidence=[]),
        "scope": ComponentScore(score=0.48, evidence=[]),
    }
    assert aggregate(components) == 0.84


def test_weights_are_complete_and_normalized() -> None:
    assert set(CANDIDATE_SCORE_WEIGHTS) == {
        "instruction",
        "verifier",
        "reconstructability",
        "isolation",
        "environment",
        "scope",
    }
    assert sum(CANDIDATE_SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_assessment_version_constant() -> None:
    assert ASSESSMENT_VERSION == "adaptive-v1"  # PRD §69


# ---------------------------------------------------------------- classify


def test_classify_default_thresholds() -> None:
    cfg = TaskMiningConfig()  # 0.80 / 0.60 (PRD §24)
    assert classify(0.80, cfg) is CandidateClass.HIGH_CONFIDENCE
    assert classify(0.79, cfg) is CandidateClass.MEDIUM_CONFIDENCE
    assert classify(0.60, cfg) is CandidateClass.MEDIUM_CONFIDENCE
    assert classify(0.59, cfg) is CandidateClass.EXPERIMENTAL


def test_classify_custom_thresholds() -> None:
    cfg = TaskMiningConfig(
        candidate_score=CandidateScoreConfig(high_confidence=0.9, medium_confidence=0.5)
    )
    assert classify(0.90, cfg) is CandidateClass.HIGH_CONFIDENCE
    assert classify(0.55, cfg) is CandidateClass.MEDIUM_CONFIDENCE
    assert classify(0.49, cfg) is CandidateClass.EXPERIMENTAL


# ---------------------------------------------------------------- risk_flags


def test_risk_flags_individual_triggers() -> None:
    assert risk_flags(_features(), percentile=82.0, n_baseline=100, test_loc=50) == ["large_diff"]
    # xlarge subsumes large — one flag per dimension keeps inspect terse.
    assert risk_flags(_features(), percentile=96.0, n_baseline=100, test_loc=50) == [
        "xlarge_diff",
    ]
    # Generated dominates: 1200 of 1700 implementation LOC are machine-written.
    assert "generated_heavy" in risk_flags(
        _features(implementation_loc=1700, generated_loc=1200), 30.0, 100, 50
    )
    assert "multi_package" in risk_flags(_features(packages_touched=3), 30.0, 100, 50)
    assert "cross_language" in risk_flags(
        _features(languages=["python", "typescript"]), 30.0, 100, 50
    )
    assert "weak_verifier_evidence" in risk_flags(_features(), 30.0, 100, test_loc=5)
    assert "small_repository_baseline" in risk_flags(_features(), 30.0, n_baseline=10, test_loc=50)


def test_risk_flags_generated_not_heavy_when_minority() -> None:
    flags = risk_flags(_features(implementation_loc=1000, generated_loc=200), 30.0, 100, 50)
    assert "generated_heavy" not in flags


def test_risk_flags_clean_change_has_none() -> None:
    features = _features(languages=["python"], packages_touched=1, dirs_touched=1)
    assert risk_flags(features, percentile=30.0, n_baseline=100, test_loc=50) == []


def test_risk_flags_sorted_and_deduplicated() -> None:
    flags = risk_flags(
        _features(packages_touched=3, languages=["python", "typescript"]),
        percentile=96.0,
        n_baseline=5,
        test_loc=2,
    )
    assert flags == sorted(flags)
    assert len(flags) == len(set(flags))
    assert flags == [
        "cross_language",
        "multi_package",
        "small_repository_baseline",
        "weak_verifier_evidence",
        "xlarge_diff",
    ]
