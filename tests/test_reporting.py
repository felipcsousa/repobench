"""Reporting tests: terminal renderer and JSON round-trip (PRD §111-112)."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest

from repobench.analysis.metrics import SegmentStat, TargetMetrics
from repobench.analysis.pareto import ParetoResult
from repobench.analysis.recommendation import Recommendation
from repobench.analysis.stats import wilson_ci
from repobench.benchmark.health import HealthReport
from repobench.cli.maintenance import RunRowView, RunShowView
from repobench.cli.render import render_run_show
from repobench.core.types import TrialOutcome, TrialResult
from repobench.reporting import (
    InstructionGenerationStats,
    PairComparison,
    ReportData,
    render_json,
    render_report,
)
from repobench.reporting.export import CSV_COLUMNS, render_csv

GENERATED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def make_target(target_id: str, rate: float, *, p50_ms: int, cost_per_solve: float | None) -> TargetMetrics:
    solved = round(rate * 10)
    wilson_lo, wilson_hi = wilson_ci(solved, 10)
    return TargetMetrics(
        target_id=target_id,
        n=10,
        solved=solved,
        solve_rate=rate,
        time_p50_ms=p50_ms,
        time_p90_ms=p50_ms * 2,
        timeouts=0,
        errors=0,
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cost_usd=cost_per_solve * 10 if cost_per_solve is not None else None,
        cost_source="HARNESS_REPORTED" if cost_per_solve is not None else None,
        cost_per_solve_usd=cost_per_solve,
        effective_cost_usd=cost_per_solve,
        mean_files_changed=2.0,
        wilson_lo=wilson_lo,
        wilson_hi=wilson_hi,
    )


def sample_report_data() -> ReportData:
    return ReportData(
        benchmark_id="rb_b_20260901_ab12",
        repository="acme/payments",
        run_id="run_abc123",
        tasks_total=24,
        health=HealthReport(
            representativeness=90,
            validation_confidence=97,
            leakage_resistance=78,
            recency=88,
            diversity=82,
            overall=86,
            warnings=["No network isolation (host-native execution)"],
        ),
        targets=[
            make_target("claude", 0.8, p50_ms=558_000, cost_per_solve=1.5),  # 9m18
            make_target("codex", 0.7, p50_ms=522_000, cost_per_solve=0.9),
            make_target("glm", 0.5, p50_ms=663_000, cost_per_solve=0.2),  # 11m03
        ],
        comparisons=[
            PairComparison(
                target_a="claude",
                target_b="codex",
                diff_pp=-10.0,
                ci_lo_pp=-40.0,
                ci_hi_pp=20.0,
                conclusive=False,
            ),
            PairComparison(
                target_a="claude",
                target_b="glm",
                diff_pp=30.0,
                ci_lo_pp=5.0,
                ci_hi_pp=55.0,
                conclusive=True,
            ),
        ],
        recommendation=Recommendation(
            best_quality_target="claude",
            candidates_not_worse=["claude", "codex"],
            recommended="codex",
            reason=(
                "codex has the lowest cost per verified solve among targets not "
                "conclusively worse than claude (best observed quality, 80% solve rate)."
            ),
        ),
        pareto=ParetoResult(frontier=["claude", "codex", "glm"], axes="quality-cost"),
        segments={
            "task_type": {
                "bugfix": {
                    "claude": SegmentStat(n=2, solved=2, rate=1.0, low_sample=True),
                    "codex": SegmentStat(n=2, solved=1, rate=0.5, low_sample=True),
                    "glm": SegmentStat(n=2, solved=1, rate=0.5, low_sample=True),
                },
                "feature": {
                    "claude": SegmentStat(n=8, solved=6, rate=0.75, low_sample=False),
                    "codex": SegmentStat(n=8, solved=6, rate=0.75, low_sample=False),
                    "glm": SegmentStat(n=8, solved=4, rate=0.4, low_sample=False),
                },
            },
            "instruction_confidence": {
                "A": {
                    "claude": SegmentStat(n=10, solved=8, rate=0.8, low_sample=False),
                    "codex": SegmentStat(n=10, solved=7, rate=0.7, low_sample=False),
                    "glm": SegmentStat(n=10, solved=5, rate=0.5, low_sample=False),
                },
            },
        },
        instruction_generation=InstructionGenerationStats(generated=3, failed=1),
        warnings=["No network isolation (host-native execution)"],
        concurrency=4,
        bootstrap_seed=42,
        generated_at=GENERATED_AT,
    )


class TestTerminalReport:
    def test_contains_core_sections(self):
        text = render_report(sample_report_data())
        assert "REPOBENCH" in text
        assert "acme/payments" in text
        assert "rb_b_20260901_ab12" in text
        assert "Tasks" in text and "24" in text
        assert "86/100" in text

    def test_target_rows_with_time_and_cost_formats(self):
        text = render_report(sample_report_data())
        # PRD-style durations and $/solve values must appear verbatim.
        assert "9m18" in text
        assert "11m03" in text
        assert "$1.50" in text
        assert "80%" in text

    def test_pairwise_comparison_rendering(self):
        text = render_report(sample_report_data())
        assert "claude vs codex" in text
        assert "Observed difference: -10pp" in text
        assert "95% CI: -40pp → +20pp" in text
        assert "No conclusive quality difference." in text
        assert "claude vs glm" in text
        assert "Observed difference: +30pp" in text
        assert "Statistically conclusive difference." in text

    def test_recommendation_rendering(self):
        text = render_report(sample_report_data())
        assert "Cost-effective recommendation:" in text
        assert "codex" in text

    def test_pareto_plot_rendering(self):
        data = sample_report_data()
        # Make codex dominated: lower quality than claude AND costlier than glm.
        data.targets[1] = data.targets[1].model_copy(
            update={
                "cost_per_solve_usd": 2.0,
                "total_cost_usd": 20.0,
                "effective_cost_usd": 2.0,
            }
        )
        data.pareto = ParetoResult(frontier=["claude", "glm"], axes="quality-cost")
        text = render_report(data)
        assert "Pareto frontier — quality × cost" in text
        assert "Frontier: claude, glm" in text
        # quality axis labels and both axis extremes ($0.20–$2.00 effective cost)
        assert "100%" in text and "  0%" in text
        assert "$0.20" in text and "$2.00" in text
        # the dominated target is still plotted, marked differently
        assert "○" in text and "●" in text

    def test_pareto_quality_time_axis_when_cost_absent(self):
        data = sample_report_data()
        for target in data.targets:
            target.total_cost_usd = None
            target.cost_per_solve_usd = None
            target.cost_source = None
            target.effective_cost_usd = None
        data.pareto = ParetoResult(frontier=["claude"], axes="quality-time")
        text = render_report(data)
        assert "Pareto frontier — quality × time" in text

    def test_instruction_tier_segment_rendering(self):
        text = render_report(sample_report_data())
        assert "Segments — instruction_confidence" in text

    def test_instruction_generation_stats_rendering(self):
        text = render_report(sample_report_data())
        assert "Instruction generation: 3 generated · 1 fallback" in text

    def test_bootstrap_seed_rendering(self):
        text = render_report(sample_report_data())
        assert "Bootstrap seed: 42" in text

    def test_low_sample_markers_and_warnings(self):
        text = render_report(sample_report_data())
        assert "Segments — task_type" in text
        assert "LOW SAMPLE" in text
        assert "Benchmark warnings:" in text
        assert "⚠ No network isolation (host-native execution)" in text

    def test_unavailable_cost_renders_placeholder(self):
        data = sample_report_data()
        # Strip cost from a single target; the other rows stay costed.
        data.targets[1] = data.targets[1].model_copy(
            update={
                "cost_per_solve_usd": None,
                "total_cost_usd": None,
                "cost_source": None,
                "effective_cost_usd": None,
            }
        )
        text = render_report(data)

        def cost_row(target_id: str) -> str:
            # The first line starting with the id is the Target table row (the
            # comparison headers come later and are phrased "a vs b").
            for line in text.splitlines():
                if line.startswith(target_id):
                    return line
            raise AssertionError(f"no target row found for {target_id!r}")

        assert "$1.50" in cost_row("claude")  # costed target renders a $ value
        assert "$0.20" in cost_row("glm")
        codex_row = cost_row("codex")
        assert "8m42" in codex_row  # duration still renders for the same target
        assert "—" in codex_row  # the unreported $/Solve cell renders the placeholder
        assert "$" not in codex_row  # and no cost figure is invented

    def test_wilson_ci_rendered_from_model_fields(self):
        # The interval comes from the fields computed in aggregation, not from a
        # renderer-side recomputation.
        data = sample_report_data()
        data.targets[0] = data.targets[0].model_copy(
            update={"wilson_lo": 0.4902, "wilson_hi": 0.9433}
        )
        text = render_report(data)
        assert "claude: 49%–94%" in text

    def test_unavailable_recommendation_renders_reason(self):
        data = sample_report_data()
        data.recommendation = Recommendation(
            best_quality_target="claude",
            candidates_not_worse=["claude", "codex"],
            recommended=None,
            reason=(
                "Economic recommendation unavailable (subscription-backed or "
                "unreported costs); best observed quality: claude (80% solve rate)."
            ),
        )
        text = render_report(data)
        assert "Economic recommendation unavailable" in text
        assert text.count("Economic recommendation") == 1

    def test_minimal_report_without_optionals(self):
        data = ReportData(
            benchmark_id=None,
            repository=None,
            run_id=None,
            tasks_total=0,
            health=None,
            targets=[],
            comparisons=[],
            recommendation=None,
            segments={},
            warnings=[],
            concurrency=None,
            generated_at=GENERATED_AT,
        )
        text = render_report(data)
        assert "REPOBENCH" in text
        assert "No conclusive quality difference." not in text
        assert "LOW SAMPLE" not in text


class TestPartialCreditSection:
    """Onda 4: per-test partial credit surfaced per target — the section only
    exists when at least one target has data, and n is always visible."""

    def test_partial_credit_renders_with_data(self):
        data = sample_report_data()
        data.targets[0] = data.targets[0].model_copy(
            update={
                "tests_partial": 0.78,
                "tests_partial_n": 14,
                "tests_mean_passed": 9.0,
                "tests_mean_denominator": 12.0,
            }
        )
        text = render_report(data)
        assert "Partial credit (hidden tests — mean passed/(total-skipped) over trials with data)" in text
        assert "claude: partial 0.78 (n=14 trials)" in text

    def test_partial_credit_section_absent_without_data(self):
        # sample targets carry no counts: nothing is invented, no section at all.
        text = render_report(sample_report_data())
        assert "Partial credit" not in text

    def test_partial_credit_target_without_data_renders_placeholder(self):
        data = sample_report_data()
        data.targets[0] = data.targets[0].model_copy(
            update={
                "tests_partial": 0.78,
                "tests_partial_n": 14,
                "tests_mean_passed": 9.0,
                "tests_mean_denominator": 12.0,
            }
        )
        text = render_report(data)
        assert "codex: — (n=0 trials)" in text
        assert "glm: — (n=0 trials)" in text


class TestJsonReport:
    def test_json_round_trip(self):
        data = sample_report_data()
        parsed = ReportData.model_validate_json(render_json(data))
        assert parsed == data

    def test_json_carries_per_target_wilson_cis(self):
        data = sample_report_data()
        parsed = ReportData.model_validate_json(render_json(data))
        assert parsed.targets[0].wilson_lo is not None
        assert parsed.targets[0].wilson_hi is not None

    def test_json_carries_partial_credit_fields(self):
        data = sample_report_data()
        data.targets[0] = data.targets[0].model_copy(
            update={
                "tests_partial": 0.78,
                "tests_partial_n": 14,
                "tests_mean_passed": 9.0,
                "tests_mean_denominator": 12.0,
            }
        )
        parsed = ReportData.model_validate_json(render_json(data))
        assert parsed.targets[0].tests_partial == pytest.approx(0.78)
        assert parsed.targets[0].tests_partial_n == 14
        assert parsed.targets[0].tests_mean_passed == pytest.approx(9.0)
        assert parsed.targets[0].tests_mean_denominator == pytest.approx(12.0)

    def test_json_carries_pareto_and_generation_and_seed(self):
        parsed = ReportData.model_validate_json(render_json(sample_report_data()))
        assert parsed.pareto is not None
        assert parsed.pareto.frontier == ["claude", "codex", "glm"]
        assert parsed.pareto.axes == "quality-cost"
        assert parsed.instruction_generation == InstructionGenerationStats(
            generated=3, failed=1
        )
        assert parsed.bootstrap_seed == 42
        assert "instruction_confidence" in parsed.segments

    def test_json_is_indented(self):
        assert "\n" in render_json(sample_report_data())


def _counted_trial(**counts: int | None) -> TrialResult:
    return TrialResult(
        trial_id="trial_1",
        run_id="run_1",
        benchmark_id="rb_b_x",
        task_id="task_1",
        target_id="claude",
        outcome=TrialOutcome.SOLVED,
        **counts,
    )


class TestCsvPerTestCounts:
    """Onda 4: the hidden-verifier per-test counts ride along in the CSV."""

    def test_csv_columns_after_tampered_tests(self):
        expected = [
            "tests_passed",
            "tests_failed",
            "tests_skipped",
            "tests_total",
            "test_report_source",
        ]
        start = CSV_COLUMNS.index("tampered_tests") + 1
        assert list(CSV_COLUMNS[start : start + 5]) == expected

    def test_csv_row_carries_counts(self):
        trial = _counted_trial(
            tests_passed=9,
            tests_failed=2,
            tests_skipped=1,
            tests_total=12,
            test_report_source="pytest-junit",
        )
        rows = list(csv.reader(io.StringIO(render_csv([trial]))))
        header, row = rows[0], rows[1]
        assert row[header.index("tests_passed")] == "9"
        assert row[header.index("tests_failed")] == "2"
        assert row[header.index("tests_skipped")] == "1"
        assert row[header.index("tests_total")] == "12"
        assert row[header.index("test_report_source")] == "pytest-junit"

    def test_csv_counts_empty_without_data(self):
        # No extracted report ⇒ empty cells, never zeros.
        rows = list(csv.reader(io.StringIO(render_csv([_counted_trial()]))))
        header, row = rows[0], rows[1]
        for column in (
            "tests_passed",
            "tests_failed",
            "tests_skipped",
            "tests_total",
            "test_report_source",
        ):
            assert row[header.index(column)] == ""


class TestRunShowTestsColumn:
    """`runs --show` gains a TESTS cell: mean passed / mean (total - skipped)."""

    @staticmethod
    def _view(metrics: TargetMetrics) -> RunShowView:
        row = RunRowView(
            run_id="run_1",
            benchmark_id="rb_b_x",
            status="COMPLETED",
            started_at="2026-09-01T12:00:00",
            finished_at="2026-09-01T12:30:00",
            targets=1,
            trials_done=10,
            trials_solved=8,
        )
        return RunShowView(row=row, targets=[metrics])

    @staticmethod
    def _target_row_line(output: str, target_id: str) -> str:
        return next(line for line in output.splitlines() if target_id in line)

    def test_tests_cell_shows_passed_over_denominator(self, capsys):
        metrics = make_target("claude", 0.8, p50_ms=558_000, cost_per_solve=1.5)
        metrics = metrics.model_copy(
            update={
                "tests_mean_passed": 9.0,
                "tests_mean_denominator": 12.0,
                "tests_partial_n": 1,
            }
        )
        render_run_show(self._view(metrics))
        out = capsys.readouterr().out
        assert "TESTS" in out
        row_line = self._target_row_line(out, "claude")
        assert "9/12" in row_line

    def test_tests_cell_placeholder_without_data(self, capsys):
        metrics = make_target("claude", 0.8, p50_ms=558_000, cost_per_solve=1.5)
        render_run_show(self._view(metrics))
        out = capsys.readouterr().out
        assert "TESTS" in out
        row_line = self._target_row_line(out, "claude")
        assert "9/12" not in row_line
        assert "—" in row_line  # the TESTS cell renders the None glyph
