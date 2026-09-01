"""Reporting tests: terminal renderer and JSON round-trip (PRD §111-112)."""

from __future__ import annotations

from datetime import datetime, timezone

from repobench.analysis.metrics import SegmentStat, TargetMetrics
from repobench.analysis.pareto import ParetoResult
from repobench.analysis.recommendation import Recommendation
from repobench.analysis.stats import wilson_ci
from repobench.benchmark.health import HealthReport
from repobench.reporting import (
    InstructionGenerationStats,
    PairComparison,
    ReportData,
    render_json,
    render_report,
)

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
