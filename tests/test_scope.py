"""Adaptive candidate mining foundations (PRD v0.9): repository-relative scope
features (PRD §17-21), adaptive config knobs (PRD §33-34, §62-65), the
adaptive-v1 assessment fields (PRD §68-69), and their storage columns."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from repobench.config import BenchmarkConfig, RepoBenchConfig, TaskMiningConfig
from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    Complexity,
    PRInfo,
    RejectionCode,
    TaskStatus,
)
from repobench.mining.scope import (
    behavioral_loc,
    complexity_from_percentile,
    distribution_stats,
    is_generated_path,
    percentile_rank,
    scope_features,
)
from repobench.storage.db import Storage

# ---------------------------------------------------------------- is_generated_path


def test_generated_dir_segment() -> None:
    assert is_generated_path("generated/graphql-types.ts")
    assert is_generated_path("src/__generated__/types.ts")
    assert is_generated_path("dist/bundle.js")
    assert is_generated_path("build/output.o")
    assert is_generated_path("codegen/api.ts")
    assert is_generated_path("vendor/lib/parser.js")


def test_generated_basename() -> None:
    for name in (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",  # case-insensitive matching
    ):
        assert is_generated_path(name), name
        assert is_generated_path(f"sub/dir/{name}"), name


def test_generated_suffix() -> None:
    assert is_generated_path("pkg/a.lock")
    assert is_generated_path("tests/__snapshots__/a.snap")
    assert is_generated_path("dist/app.min.js")
    assert is_generated_path("src/app.js.map")


def test_not_generated() -> None:
    # The PRD §20 pair: real source vs codegen output in a generated dir.
    assert not is_generated_path("src/payment.ts")
    assert is_generated_path("generated/graphql-types.ts")
    # A lookalike segment is not a match: "generator" != "generated".
    assert not is_generated_path("src/generator/manual.py")
    assert not is_generated_path("README.md")


# ---------------------------------------------------------------- scope_features


def test_scope_features_mixed_change() -> None:
    """PRD §20 example shape: 1700 implementation LOC of which 1200 generated,
    plus tests and a binary file (numstat -1 -1 contributes 0 LOC)."""
    changes = [
        (400, 100, "src/payment.ts"),
        (1000, 200, "generated/graphql-types.ts"),
        (30, 0, "tests/payment.test.ts"),
        (-1, -1, "assets/logo.png"),
    ]
    features = scope_features(changes)
    assert features.implementation_loc == 1700
    assert features.generated_loc == 1200
    assert features.test_loc == 30
    assert features.implementation_files == 3
    assert features.test_files == 1
    assert features.dirs_touched == 3  # src, generated, assets
    assert features.packages_touched == 3  # src, generated, assets
    assert features.languages == ["typescript"]  # .png suffix is unknown -> omitted
    assert features.impl_paths == [
        "assets/logo.png",
        "generated/graphql-types.ts",
        "src/payment.ts",
    ]
    # Non-generated 500 + 20% of 1200 = 740 — never a 1700-LOC task.
    assert behavioral_loc(features) == 740


def test_scope_features_root_files_and_unknown_language() -> None:
    features = scope_features([(5, 0, "README.md"), (2, 1, "Makefile")])
    assert features.implementation_loc == 8
    assert features.generated_loc == 0
    assert features.dirs_touched == 1  # both at the repository root
    assert features.packages_touched == 2
    assert features.languages == []  # no known implementation suffix
    assert behavioral_loc(features) == 8


def test_behavioral_loc_floor() -> None:
    # 20% weight floors at int: 7 generated LOC -> 1, and the result never dips
    # below the non-generated LOC.
    features = scope_features([(7, 0, "generated/api.ts")])
    assert behavioral_loc(features) == 1


# ---------------------------------------------------------------- percentiles


def test_percentile_rank_empty_values_is_neutral() -> None:
    # No repository baseline exists yet: 50.0, not an invented P0/P100.
    assert percentile_rank(900.0, []) == 50.0


def test_percentile_rank_known_distribution() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile_rank(10.0, values) == 25.0
    assert percentile_rank(25.0, values) == 50.0
    assert percentile_rank(40.0, values) == 100.0
    assert percentile_rank(5.0, values) == 0.0


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0, Complexity.SMALL),
        (34.9, Complexity.SMALL),
        (35, Complexity.MEDIUM),  # boundary belongs to the higher bucket
        (79.9, Complexity.MEDIUM),
        (80, Complexity.LARGE),
        (94.9, Complexity.LARGE),
        (95, Complexity.XLARGE),
        (100, Complexity.XLARGE),
    ],
)
def test_complexity_from_percentile(percentile: float, expected: Complexity) -> None:
    assert complexity_from_percentile(percentile) is expected


def test_distribution_stats_nearest_rank() -> None:
    # Nearest-rank: each quantile is a value that actually exists in the list.
    values = [40.0, 10.0, 30.0, 20.0]  # unsorted input, deterministic output
    assert distribution_stats(values) == {"p50": 20.0, "p75": 30.0, "p90": 40.0}
    assert distribution_stats([]) == {"p50": 0.0, "p75": 0.0, "p90": 0.0}


# ---------------------------------------------------------------- config


def test_task_mining_defaults_stay_hard_caps() -> None:
    # PRD §34/§77: the default STAYS hard_caps in this PR; adaptive knobs only
    # add surface, never flip behavior.
    cfg = TaskMiningConfig()
    assert cfg.size_policy == "hard_caps"
    assert cfg.complexity.method == "absolute"
    assert cfg.candidate_score.high_confidence == 0.80
    assert cfg.candidate_score.medium_confidence == 0.60
    assert cfg.validation.include is None
    assert cfg.extreme_scope_loc == 5000
    assert cfg.extreme_scope_files == 200


def test_benchmark_config_defaults() -> None:
    cfg = BenchmarkConfig()
    assert cfg.min_size == 10
    assert cfg.min_representativeness is None
    assert cfg.validation_budget.max_candidates is None


def test_legacy_flat_yaml_still_validates(tmp_path: Path) -> None:
    """PRD §34/§67: a pre-adaptive repobench.yml (flat hard-cap keys only)
    must keep loading with every legacy default intact."""
    path = tmp_path / "repobench.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "task_mining": {
                    "require_test_change": True,
                    "min_implementation_loc": 20,
                    "max_implementation_loc": 400,
                    "max_implementation_files": 8,
                }
            }
        )
    )
    cfg = RepoBenchConfig.load(path)
    assert cfg.task_mining.max_implementation_loc == 400
    assert cfg.task_mining.size_policy == "hard_caps"
    assert cfg.task_mining.candidate_score.high_confidence == 0.80


def test_adaptive_nested_config_validates() -> None:
    cfg = RepoBenchConfig.model_validate(
        {
            "task_mining": {
                "size_policy": "adaptive",
                "complexity": {"method": "repository_percentile"},
                "candidate_score": {"high_confidence": 0.9, "medium_confidence": 0.5},
                "validation": {"include": ["high", "medium"]},
                "extreme_scope_loc": 20000,
            },
            "benchmark": {
                "min_size": 5,
                "min_representativeness": 75,
                "validation_budget": {"max_candidates": 60},
            },
        }
    )
    assert cfg.task_mining.size_policy == "adaptive"
    assert cfg.task_mining.complexity.method == "repository_percentile"
    assert cfg.task_mining.candidate_score.high_confidence == 0.9
    assert cfg.task_mining.validation.include == ["high", "medium"]
    assert cfg.task_mining.extreme_scope_loc == 20000
    assert cfg.benchmark.min_size == 5
    assert cfg.benchmark.min_representativeness == 75
    assert cfg.benchmark.validation_budget.max_candidates == 60


# ---------------------------------------------------------------- types backward compat


def test_assessment_legacy_dict_validates_with_defaults() -> None:
    """Only the original 11 mining fields: the adaptive-v1 fields (PRD §68)
    must default so persisted data_json rows keep validating."""
    legacy = Assessment.model_validate(
        {
            "task_type": "bugfix",
            "subsystem": "src/payments",
            "complexity": "small",
            "language": None,
            "instruction": "Fix charge rounding",
            "instruction_confidence": "B",
            "instruction_source": "pr_body",
            "implementation_loc": 6,
            "test_loc": 2,
            "implementation_files": 1,
            "test_files": 1,
        }
    )
    assert legacy.candidate_score is None
    assert legacy.candidate_class is None
    for score in (
        legacy.instruction_score,
        legacy.verifier_score,
        legacy.reconstructability_score,
        legacy.scope_score,
        legacy.isolation_score,
        legacy.environment_score,
        legacy.scope_percentile,
    ):
        assert score is None
    assert legacy.generated_loc is None and legacy.behavioral_loc is None
    assert legacy.dirs_touched is None
    assert legacy.languages_touched == [] and legacy.risk_flags == []
    assert legacy.assessment_version is None


def test_assessment_legacy_json_roundtrip() -> None:
    legacy_json = Assessment(
        implementation_loc=6, test_loc=2, instruction="Fix charge"
    ).model_dump_json()
    restored = Assessment.model_validate_json(legacy_json)
    assert restored.candidate_class is None and restored.candidate_score is None


def test_new_enum_members() -> None:
    assert CandidateClass.HIGH_CONFIDENCE.value == "HIGH_CONFIDENCE"
    assert CandidateClass.MEDIUM_CONFIDENCE.value == "MEDIUM_CONFIDENCE"
    assert CandidateClass.EXPERIMENTAL.value == "EXPERIMENTAL"
    assert Complexity.XLARGE.value == "xlarge"
    assert TaskStatus.LOW_PRIORITY.value == "LOW_PRIORITY"
    assert RejectionCode.NO_IMPLEMENTATION_CHANGE.value == "NO_IMPLEMENTATION_CHANGE"
    assert RejectionCode.TASK_EXTREME_SCOPE.value == "TASK_EXTREME_SCOPE"
    # Legacy hard-cap codes stay available (PRD §38).
    assert RejectionCode.TASK_TOO_LARGE.value == "TASK_TOO_LARGE"
    assert RejectionCode.TASK_TOO_SMALL.value == "TASK_TOO_SMALL"


# ---------------------------------------------------------------- storage (PRD §68)


def _candidate(candidate_id: str, number: int, **assessment_kwargs) -> CandidateInfo:
    return CandidateInfo(
        candidate_id=candidate_id,
        pr=PRInfo(number=number),
        assessment=Assessment(implementation_loc=6, **assessment_kwargs),
    )


def test_save_candidate_roundtrips_class_and_score(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.db")
    scored = _candidate(
        "c_1_a",
        1,
        candidate_class=CandidateClass.HIGH_CONFIDENCE,
        candidate_score=0.91,
    )
    unscored = _candidate("c_2_b", 2)  # legacy hard_caps mining: no class/score
    storage.save_candidate(scored)
    storage.save_candidate(unscored)

    loaded = {c.candidate_id: c for c in storage.list_candidates()}
    assert loaded["c_1_a"].assessment.candidate_class is CandidateClass.HIGH_CONFIDENCE
    assert loaded["c_1_a"].assessment.candidate_score == 0.91
    assert loaded["c_2_b"].assessment.candidate_class is None
    assert loaded["c_2_b"].assessment.candidate_score is None

    # The raw columns exist and distinguish scored from legacy rows without
    # decoding data_json (PRD §68).
    rows = {
        row["candidate_id"]: row
        for row in storage.query(
            "SELECT candidate_id, candidate_class, candidate_score FROM candidates"
        )
    }
    assert rows["c_1_a"]["candidate_class"] == "HIGH_CONFIDENCE"
    assert rows["c_1_a"]["candidate_score"] == 0.91
    assert rows["c_2_b"]["candidate_class"] is None
    assert rows["c_2_b"]["candidate_score"] is None


def test_migrate_candidates_table_from_legacy_schema(tmp_path: Path) -> None:
    """A database created before adaptive mining (no candidate_class /
    candidate_score, no trials.rollout) must migrate in place and keep its
    legacy candidate rows readable (PRD §68)."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE candidates(
          candidate_id TEXT PRIMARY KEY,
          pr_number INTEGER,
          status TEXT,
          rejection_code TEXT,
          task_type TEXT,
          subsystem TEXT,
          complexity TEXT,
          data_json TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE trials(
          trial_id TEXT PRIMARY KEY,
          run_id TEXT,
          benchmark_id TEXT,
          task_id TEXT,
          target_id TEXT,
          outcome TEXT,
          data_json TEXT,
          created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO candidates VALUES ('c_old', 7, 'DISCOVERED', NULL, 'bugfix',"
        " 'src', 'small', '{}', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    storage = Storage(db_path)  # opens and migrates in place
    columns = {
        row["name"]
        for row in storage.query("PRAGMA table_info(candidates)")
    }
    assert {"candidate_class", "candidate_score"} <= columns
    assert {row["name"] for row in storage.query("PRAGMA table_info(trials)")} >= {"rollout"}

    # Legacy row survives untouched (its data_json predates the model); new
    # scored rows write through the migrated schema and decode fine.
    assert storage.query("SELECT candidate_id FROM candidates WHERE candidate_id = 'c_old'")
    storage.save_candidate(
        _candidate("c_new", 8, candidate_class=CandidateClass.EXPERIMENTAL, candidate_score=0.4)
    )
    rows = storage.query("SELECT candidate_id FROM candidates")
    assert len(rows) == 2
    migrated = storage.candidates_for_pr(8)
    assert len(migrated) == 1
    assert migrated[0].assessment.candidate_class is CandidateClass.EXPERIMENTAL
