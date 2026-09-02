"""Wave 3 benchmark credibility (issue #19): verifier-strength scoring.

Three pieces: flakiness estimated from the append-only validation history (a
task is flaky when the same check kind produced both a passed and a failed row
across builds), a brittle-assertion linter over verifier diffs (warnings only,
never scores), and the verifier-strength component in Benchmark Health with
rebalanced weights (documented deviation from PRD §86).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from repobench.benchmark.health import (
    _WEIGHT_DIVERSITY,
    _WEIGHT_LEAKAGE,
    _WEIGHT_RECENCY,
    _WEIGHT_REPRESENTATIVENESS,
    _WEIGHT_VALIDATION,
    _WEIGHT_VERIFIER_STRENGTH,
    HealthReport,
    compute_health,
)
from repobench.benchmark.coverage import CoverageReport
from repobench.cli.builds import _brittle_findings
from repobench.cli.reports import build_report_data
from repobench.cli.render import render_benchmark_build
from repobench.config import RepoBenchConfig
from repobench.core.types import TrialOutcome, TrialResult
from repobench.storage.db import Storage
from repobench.validation.brittle import (
    BRITTLE_FINDINGS_CAP,
    brittle_assertions,
    brittle_file_warnings,
)
from repobench.validation.flakiness import flakiness_from_history

from tests.test_e2e import _fast_forward, _invoke

runner = CliRunner()

RUN_ID = "run_w3_verifier"


def _row(task_id: str, kind: str, result: str) -> dict:
    return {
        "task_id": task_id,
        "kind": kind,
        "result": result,
        "details_json": None,
        "created_at": "2026-09-01T00:00:00",
    }


# ------------------------------------------------------- flakiness (issue #19)


class TestFlakinessFromHistory:
    def test_flip_flop_same_kind_is_flaky(self):
        rows = [_row("t1", "oracle", "passed"), _row("t1", "oracle", "failed")]
        report = flakiness_from_history(rows)
        assert report.flaky_tasks == ["t1"]
        assert report.flaky_ratio == 1.0
        assert report.tasks_seen == 1

    def test_passed_only_history_is_clean(self):
        rows = [_row("t1", "oracle", "passed"), _row("t1", "oracle", "passed")]
        report = flakiness_from_history(rows)
        assert report.flaky_tasks == []
        assert report.flaky_ratio == 0.0
        assert report.tasks_seen == 1

    def test_failed_then_failed_is_clean(self):
        rows = [_row("t1", "noop", "failed"), _row("t1", "noop", "failed")]
        assert flakiness_from_history(rows).flaky_tasks == []

    def test_different_kinds_do_not_cross_contaminate(self):
        rows = [_row("t1", "oracle", "passed"), _row("t1", "noop", "failed")]
        report = flakiness_from_history(rows)
        assert report.flaky_tasks == []
        assert report.tasks_seen == 1  # the task itself is still judged

    def test_ratio_over_distinct_tasks(self):
        rows = [
            _row("t1", "oracle", "passed"),
            _row("t1", "oracle", "failed"),
            _row("t2", "oracle", "passed"),
            _row("t3", "oracle", "failed"),
            _row("t4", "oracle", "passed"),
        ]
        report = flakiness_from_history(rows)
        assert report.flaky_tasks == ["t1"]
        assert report.flaky_ratio == 0.25
        assert report.tasks_seen == 4

    def test_flaky_tasks_are_sorted(self):
        rows = [
            _row("t2", "oracle", "passed"),
            _row("t2", "oracle", "failed"),
            _row("t1", "noop", "passed"),
            _row("t1", "noop", "failed"),
        ]
        assert flakiness_from_history(rows).flaky_tasks == ["t1", "t2"]

    def test_empty_history_is_no_signal_not_quality(self):
        report = flakiness_from_history([])
        assert report.flaky_tasks == []
        assert report.flaky_ratio == 0.0
        assert report.tasks_seen == 0

    def test_skipped_only_rows_cannot_be_judged(self):
        # skipped = check-level skip (e.g. leakage None): no verdict, so the
        # task stays out of the denominator instead of silently counting clean.
        report = flakiness_from_history([_row("t1", "determinism", "skipped")])
        assert report.flaky_tasks == []
        assert report.tasks_seen == 0
        assert report.flaky_ratio == 0.0


# ---------------------------------------------------------- brittle (issue #19)

DOUBLE_QUOTE_DIFF = """\
diff --git a/tests/test_pay.py b/tests/test_pay.py
--- a/tests/test_pay.py
+++ b/tests/test_pay.py
@@ -1,3 +1,4 @@
+    assert result == "Payment successful"
"""

SINGLE_QUOTE_DIFF = """\
diff --git a/tests/test_pay.py b/tests/test_pay.py
--- a/tests/test_pay.py
+++ b/tests/test_pay.py
@@ -1,3 +1,4 @@
+    assert result == 'Payment successful'
"""


class TestBrittleAssertions:
    def test_exact_string_double_quotes_with_file_prefix(self):
        assert brittle_assertions(DOUBLE_QUOTE_DIFF) == [
            'tests/test_pay.py: assert result == "Payment successful"'
        ]

    def test_exact_string_single_quotes(self):
        assert brittle_assertions(SINGLE_QUOTE_DIFF) == [
            "tests/test_pay.py: assert result == 'Payment successful'"
        ]

    def test_short_literals_are_not_flagged(self):
        diff = DOUBLE_QUOTE_DIFF.replace(
            '"Payment successful"', '"ok"'
        ).replace('"Payment successful"', '"ok"')
        assert brittle_assertions(diff) == []

    def test_assert_equal_style_flagged(self):
        diff = DOUBLE_QUOTE_DIFF.replace(
            'assert result == "Payment successful"',
            'self.assertEqual(fmt(total), "Payment successful")',
        )
        assert brittle_assertions(diff) == [
            'tests/test_pay.py: self.assertEqual(fmt(total), "Payment successful")'
        ]

    def test_assert_true_with_comparison_flagged(self):
        diff = DOUBLE_QUOTE_DIFF.replace(
            'assert result == "Payment successful"',
            'assertTrue(status == "Payment successful")',
        )
        assert len(brittle_assertions(diff)) == 1

    def test_removed_and_context_lines_are_ignored(self):
        diff = DOUBLE_QUOTE_DIFF.replace("+    assert", "-    assert")
        assert brittle_assertions(diff) == []
        context_only = DOUBLE_QUOTE_DIFF.replace("+    assert", "     assert")
        assert brittle_assertions(context_only) == []

    def test_implementation_only_diff_yields_nothing(self):
        diff = """\
diff --git a/src/pay.py b/src/pay.py
--- a/src/pay.py
+++ b/src/pay.py
@@ -1,2 +1,2 @@
-    return "declined"
+    return "approved 12345678"
"""
        assert brittle_assertions(diff) == []

    def test_empty_and_headerless_text_yields_nothing(self):
        assert brittle_assertions("") == []
        # added lines before any +++ header have no file to attribute to
        assert brittle_assertions('+ assert x == "Payment successful"') == []

    def test_file_attribution_follows_hunk_headers(self):
        diff = (
            DOUBLE_QUOTE_DIFF
            + """\
diff --git a/tests/test_other.py b/tests/test_other.py
--- a/tests/test_other.py
+++ b/tests/test_other.py
@@ -1,2 +1,3 @@
+    assert result == "Payment successful"
"""
        )
        findings = brittle_assertions(diff)
        assert [f.split(":")[0] for f in findings] == [
            "tests/test_pay.py",
            "tests/test_other.py",
        ]

    def test_duplicate_added_lines_reported_once(self):
        assert brittle_assertions(DOUBLE_QUOTE_DIFF * 3) == [
            'tests/test_pay.py: assert result == "Payment successful"'
        ]

    def test_file_warnings_collapse_to_one_per_file(self):
        warnings = brittle_file_warnings(
            [
                "tests/test_b.py: assert x == \"abcdef\"",
                "tests/test_a.py: assert y == \"abcdef\"",
                "tests/test_b.py: assert z == \"abcdef\"",
            ]
        )
        assert warnings == [
            "brittle exact-string assertions in tests/test_b.py",
            "brittle exact-string assertions in tests/test_a.py",
        ]

    def test_build_collection_caps_findings(self):
        diffs = [
            DOUBLE_QUOTE_DIFF.replace("test_pay.py", f"test_{i:02d}.py") for i in range(20)
        ]
        findings = _brittle_findings(diffs)
        assert len(findings) == BRITTLE_FINDINGS_CAP


# ------------------------------------------------ compute_health (issue #19)


def _health(**overrides):
    base = dict(
        coverage=CoverageReport(task_type=90, subsystem=90, complexity=90, overall=90),
        all_checks_passed_ratio=1.0,
        leakage_score=90,
        tasks=[],
        now=None,
    )
    base.update(overrides)
    return compute_health(**base)


class TestVerifierStrengthComponent:
    def test_rebalanced_weights_still_sum_to_one(self):
        total = (
            _WEIGHT_REPRESENTATIVENESS
            + _WEIGHT_VALIDATION
            + _WEIGHT_VERIFIER_STRENGTH
            + _WEIGHT_LEAKAGE
            + _WEIGHT_RECENCY
            + _WEIGHT_DIVERSITY
        )
        assert math.isclose(total, 1.0)

    def test_verifier_strength_score_math(self):
        health = _health(flaky_tasks=["t1"], total_validated_tasks=4)
        assert health.verifier_strength == 75  # 1 of 4 tasks flip-flopped

        never_flipped = _health(flaky_tasks=[], total_validated_tasks=4)
        assert never_flipped.verifier_strength == 100

    def test_no_history_is_no_signal_and_scores_full(self):
        health = _health()
        assert health.verifier_strength == 100
        assert not any("flaky" in w for w in health.warnings)

    def test_flaky_and_brittle_warnings_are_emitted(self):
        health = _health(
            flaky_tasks=["t1", "t2"],
            total_validated_tasks=5,
            brittle_warnings=["brittle exact-string assertions in tests/test_pay.py"],
        )
        assert (
            "2 task(s) with flaky validation history "
            "(outcome flipped between builds)"
        ) in health.warnings
        assert "brittle exact-string assertions in tests/test_pay.py" in health.warnings

    def test_old_persisted_health_json_still_loads(self):
        # A benchmark frozen before issue #19 has no verifier_strength field;
        # loading it must keep the report, not null it (honesty: never invent).
        old_json = json.dumps(
            {
                "representativeness": 90,
                "validation_confidence": 90,
                "leakage_resistance": 90,
                "recency": 50,
                "diversity": 50,
                "overall": 84,
                "warnings": [],
            }
        )
        loaded = HealthReport.model_validate_json(old_json)
        assert loaded.verifier_strength is None
        assert loaded.overall == 84

    def test_build_report_data_keeps_health_without_the_new_field(
        self, tmp_path: Path
    ):
        storage = Storage(tmp_path / "state.db")
        old_json = HealthReport(
            representativeness=90,
            validation_confidence=90,
            leakage_resistance=90,
            recency=50,
            diversity=50,
            overall=84,
            warnings=[],
        ).model_dump_json()
        storage.create_run(RUN_ID, "rb_b_w3")
        storage.save_benchmark(
            "rb_b_w3",
            size=1,
            health_json=old_json,
            manifest_path=None,
            methodology_version="v1",
        )
        storage.save_trial(
            TrialResult(
                trial_id="tr_1",
                run_id=RUN_ID,
                benchmark_id="rb_b_w3",
                task_id="t1",
                target_id="fixer",
                outcome=TrialOutcome.SOLVED,
            )
        )
        data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)
        assert data.health is not None
        assert data.health.verifier_strength is None


# --------------------------------------------------- storage reader (issue #19)


class TestValidationHistory:
    def test_returns_rows_in_insertion_order(self, tmp_path: Path):
        storage = Storage(tmp_path / "state.db")
        expected = [
            ("t1", "oracle", "passed"),
            ("t1", "determinism", "passed"),
            ("t2", "noop", "failed"),
            ("t1", "oracle", "failed"),
        ]
        for task_id, kind, result in expected:
            storage.save_validation(task_id, kind, result)
        rows = storage.validation_history()
        assert [(r["task_id"], r["kind"], r["result"]) for r in rows] == expected
        assert set(rows[0]) == {
            "task_id",
            "kind",
            "result",
            "details_json",
            "created_at",
        }

    def test_task_filter_keeps_insertion_order(self, tmp_path: Path):
        storage = Storage(tmp_path / "state.db")
        storage.save_validation("t1", "oracle", "passed")
        storage.save_validation("t2", "oracle", "failed")
        storage.save_validation("t1", "oracle", "failed")
        rows = storage.validation_history("t1")
        assert [(r["kind"], r["result"]) for r in rows] == [
            ("oracle", "passed"),
            ("oracle", "failed"),
        ]


# ------------------------------------------------------------------ e2e-lite


def _latest_health(storage: Storage) -> HealthReport:
    newest = storage.list_benchmarks()[0]  # ordered by created_at DESC
    return HealthReport.model_validate_json(newest["health_json"])


def _shipped_task_ids(storage: Storage) -> list[str]:
    newest = storage.list_benchmarks()[0]
    return storage.benchmark_task_ids(newest["benchmark_id"])


class TestVerifierStrengthInBuild:
    def test_clean_rebuild_scores_100_without_flaky_warning(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert len(_shipped_task_ids(storage)) == 1

        built = _invoke("benchmark", "build", "--reuse-valid")
        assert built.exit_code == 0, built.output
        assert "Verifier strength" in built.output
        assert "flaky validation history" not in built.output
        health = _latest_health(storage)
        # the fixture never flip-flops, so the component tops out
        assert health.verifier_strength == 100
        assert not any("flaky" in w for w in health.warnings)

    def test_flipped_history_warns_and_lowers_verifier_strength(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_id = _shipped_task_ids(storage)[0]
        passed_rows = storage.validation_history(task_id)
        assert any(r["kind"] == "determinism" and r["result"] == "passed" for r in passed_rows)

        # corrupt the append-only log the way a flaky verifier would have:
        # the same (task, kind) now holds both verdicts
        storage.save_validation(task_id, "determinism", "failed")

        built = _invoke("benchmark", "build")
        assert built.exit_code == 0, built.output
        assert "flaky validation history" in built.output
        health = _latest_health(storage)
        assert health.verifier_strength < 100
        assert any(
            "1 task(s) with flaky validation history" in w for w in health.warnings
        )

    def test_render_shows_dash_for_predates_component(self, capsys) -> None:
        outcome = SimpleNamespace(
            valid=[],
            reuse_valid=False,
            reused=0,
            requested_size=1,
            sample=[],
            instruction_tiers={},
            benchmark_id="rb_b_old",
            manifest_path=Path("/tmp/manifest.json"),
            health=HealthReport(
                representativeness=90,
                validation_confidence=90,
                verifier_strength=None,
                leakage_resistance=90,
                recency=50,
                diversity=50,
                overall=84,
                warnings=[],
            ),
        )
        render_benchmark_build(outcome)
        text = capsys.readouterr().out
        assert "Verifier strength" in text
        assert "—" in text
