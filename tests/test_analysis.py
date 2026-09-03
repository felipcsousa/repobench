"""Analysis tests: aggregation, Wilson CI, paired bootstrap, Pareto, recommendation."""

from __future__ import annotations

import pytest

from repobench.analysis import (
    SegmentStat,
    TargetMetrics,
    aggregate_trials,
    paired_bootstrap,
    pareto_frontier,
    recommend,
    segment_breakdown,
    wilson_ci,
)
from repobench.core.types import (
    Assessment,
    TaskMetadata,
    TaskType,
    TrialOutcome,
    TrialResult,
    UsageRecord,
)

N_TRIALS = 10  # paired bootstrap task count


def make_trial(
    task_id: str,
    target_id: str,
    *,
    outcome: TrialOutcome = TrialOutcome.SOLVED,
    duration_ms: int = 1000,
    usage: UsageRecord | None = None,
    cost_usd: float | None = None,
    cost_source: str | None = None,
    files_changed: int | None = None,
    tests_passed: int | None = None,
    tests_failed: int | None = None,
    tests_skipped: int | None = None,
    tests_total: int | None = None,
) -> TrialResult:
    return TrialResult(
        trial_id=f"{target_id}:{task_id}",
        task_id=task_id,
        target_id=target_id,
        outcome=outcome,
        duration_ms=duration_ms,
        usage=usage,
        cost_usd=cost_usd,
        cost_source=cost_source,
        files_changed=files_changed,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        tests_skipped=tests_skipped,
        tests_total=tests_total,
    )


def make_task(task_id: str, task_type: TaskType = TaskType.BUGFIX, subsystem: str = "core"):
    return TaskMetadata(
        task_id=task_id,
        base_sha="b" * 40,
        gold_sha="g" * 40,
        assessment=Assessment(task_type=task_type, subsystem=subsystem),
    )


class TestAggregateTrials:
    def test_solve_rate_percentiles_and_counts(self):
        trials = [
            make_trial("t1", "A", outcome=TrialOutcome.SOLVED, duration_ms=400),
            make_trial("t2", "A", outcome=TrialOutcome.SOLVED, duration_ms=100),
            make_trial("t3", "A", outcome=TrialOutcome.SOLVED, duration_ms=300),
            make_trial("t4", "A", outcome=TrialOutcome.TIMEOUT, duration_ms=200),
            make_trial("t1", "B", outcome=TrialOutcome.SOLVED, duration_ms=100),
            make_trial("t2", "B", outcome=TrialOutcome.HARNESS_ERROR, duration_ms=100),
            make_trial("t3", "B", outcome=TrialOutcome.UNSOLVED, duration_ms=100),
        ]
        metrics = aggregate_trials(trials)
        a = metrics["A"]
        assert (a.n, a.solved, a.timeouts, a.errors) == (4, 3, 1, 0)
        assert a.solve_rate == pytest.approx(0.75)
        # Nearest-rank percentiles over sorted [100, 200, 300, 400].
        assert a.time_p50_ms == 200
        assert a.time_p90_ms == 400
        # Wilson CIs are computed in aggregation and carried on the model.
        lo, hi = wilson_ci(3, 4)
        assert a.wilson_lo == pytest.approx(lo)
        assert a.wilson_hi == pytest.approx(hi)
        b = metrics["B"]
        assert (b.n, b.solved, b.errors) == (3, 1, 1)
        assert b.solve_rate == pytest.approx(1 / 3)
        assert b.time_p50_ms == 100
        assert b.time_p90_ms == 100

    def test_token_and_cost_aggregation(self):
        trials = [
            make_trial(
                "t1", "A",
                outcome=TrialOutcome.SOLVED,
                usage=UsageRecord(input_tokens=100, output_tokens=50),
                cost_usd=0.10,
                cost_source="HARNESS_REPORTED",
                files_changed=2,
            ),
            make_trial(
                "t2", "A",
                outcome=TrialOutcome.SOLVED,
                usage=UsageRecord(input_tokens=200, output_tokens=60),
                cost_usd=0.30,
                cost_source="HARNESS_REPORTED",
                files_changed=4,
            ),
            make_trial(
                "t3", "A",
                outcome=TrialOutcome.UNSOLVED,
                usage=UsageRecord(input_tokens=None, output_tokens=70),
                cost_usd=0.20,
                cost_source="HARNESS_REPORTED",
                files_changed=None,
            ),
            make_trial("t4", "A", outcome=TrialOutcome.UNSOLVED, files_changed=2),
        ]
        a = aggregate_trials(trials)["A"]
        # Sums cover present values only; every counted trial has cost -> total valid.
        assert a.total_input_tokens == 300
        assert a.total_output_tokens == 180
        assert a.total_cost_usd == pytest.approx(0.60)
        assert a.cost_source == "HARNESS_REPORTED"
        assert a.cost_per_solve_usd == pytest.approx(0.30)  # 0.60 / 2 solves
        assert a.effective_cost_usd == pytest.approx(0.30)  # per-solve when reported
        assert a.mean_files_changed == pytest.approx(8 / 3)

    def test_partial_cost_propagates_to_none(self):
        trials = [
            make_trial("t1", "B", usage=UsageRecord(input_tokens=10, output_tokens=5), cost_usd=None),
            make_trial("t2", "B", usage=UsageRecord(input_tokens=20, output_tokens=8), cost_usd=0.5),
        ]
        b = aggregate_trials(trials)["B"]
        assert b.total_input_tokens == 30
        assert b.total_output_tokens == 13
        assert b.total_cost_usd is None
        assert b.cost_source is None
        assert b.cost_per_solve_usd is None
        assert b.effective_cost_usd is None

    def test_no_usage_at_all(self):
        trials = [make_trial("t1", "C", outcome=TrialOutcome.UNSOLVED)]
        c = aggregate_trials(trials)["C"]
        assert c.total_input_tokens is None
        assert c.total_output_tokens is None
        assert c.total_cost_usd is None
        assert c.cost_per_solve_usd is None
        assert c.mean_files_changed is None

    def test_cost_per_solve_none_when_no_solves(self):
        trials = [make_trial("t1", "D", outcome=TrialOutcome.UNSOLVED, usage=UsageRecord(input_tokens=1), cost_usd=0.5)]
        d = aggregate_trials(trials)["D"]
        assert d.total_cost_usd == pytest.approx(0.5)
        assert d.cost_per_solve_usd is None
        assert d.effective_cost_usd == pytest.approx(0.5)  # falls back to the total

    def test_partial_credit_mean_over_trials_with_counts(self):
        # Onda 4: mean of passed/(total-skipped) over the trials that carry
        # counts — 9/(12-1) = 0.818 and 6/(12-2) = 0.6 — while the count-less
        # trial contributes nothing (numbers are never invented).
        trials = [
            make_trial("t1", "A", tests_passed=9, tests_failed=2, tests_skipped=1, tests_total=12),
            make_trial("t2", "A", tests_passed=6, tests_failed=4, tests_skipped=2, tests_total=12),
            make_trial("t3", "A"),
        ]
        a = aggregate_trials(trials)["A"]
        assert a.tests_partial_n == 2
        assert a.tests_partial == pytest.approx((9 / 11 + 6 / 10) / 2)
        assert a.tests_mean_passed == pytest.approx(7.5)
        assert a.tests_mean_denominator == pytest.approx(10.5)

    def test_partial_credit_none_when_no_trial_has_counts(self):
        a = aggregate_trials([make_trial("t1", "A")])["A"]
        assert a.tests_partial is None
        assert a.tests_partial_n == 0
        assert a.tests_mean_passed is None
        assert a.tests_mean_denominator is None

    def test_all_skipped_suite_is_not_data(self):
        # total == skipped means no real test ran (denominator <= 0): the trial
        # stays out of the mean and out of n, so nothing fake is averaged in.
        trials = [
            make_trial("t1", "A", tests_passed=0, tests_failed=0, tests_skipped=5, tests_total=5),
            make_trial("t2", "A", tests_passed=3, tests_failed=0, tests_skipped=0, tests_total=3),
        ]
        a = aggregate_trials(trials)["A"]
        assert a.tests_partial_n == 1
        assert a.tests_partial == pytest.approx(1.0)
        assert a.tests_mean_passed == pytest.approx(3.0)
        assert a.tests_mean_denominator == pytest.approx(3.0)


class TestSegmentBreakdown:
    def test_segments_and_low_sample_flag(self):
        tasks = {
            "t1": make_task("t1", TaskType.BUGFIX, "core"),
            "t2": make_task("t2", TaskType.BUGFIX, "api"),
            "t3": make_task("t3", TaskType.FEATURE, "core"),
            "t4": make_task("t4", TaskType.BUGFIX, "core"),
        }
        trials = [
            make_trial("t1", "A", outcome=TrialOutcome.SOLVED),
            make_trial("t2", "A", outcome=TrialOutcome.UNSOLVED),
            make_trial("t3", "A", outcome=TrialOutcome.UNSOLVED),
            make_trial("t4", "A", outcome=TrialOutcome.SOLVED),
        ]
        by_type = segment_breakdown(trials, tasks, "task_type")
        assert set(by_type) == {"A"}
        bugfix = by_type["A"]["bugfix"]
        assert (bugfix.n, bugfix.solved) == (3, 2)
        assert bugfix.rate == pytest.approx(2 / 3)
        assert bugfix.low_sample is True  # n < 5
        feature = by_type["A"]["feature"]
        assert feature.n == 1
        assert feature.low_sample is True

        by_subsystem = segment_breakdown(trials, tasks, "subsystem")
        assert by_subsystem["A"]["core"].n == 3
        assert by_subsystem["A"]["api"].n == 1

    def test_five_trials_not_low_sample(self):
        tasks = {f"t{i}": make_task(f"t{i}") for i in range(5)}
        trials = [make_trial(f"t{i}", "A", outcome=TrialOutcome.SOLVED) for i in range(5)]
        stat = segment_breakdown(trials, tasks, "task_type")["A"]["bugfix"]
        assert (stat.n, stat.low_sample) == (5, False)

    def test_unknown_dimension_raises(self):
        with pytest.raises(ValueError):
            segment_breakdown([], {}, "language")


class TestWilson:
    def test_zero_trials(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)

    def test_interval_contains_point_estimate(self):
        for solved, n in [(3, 10), (7, 10), (1, 5), (5, 5), (0, 8)]:
            lo, hi = wilson_ci(solved, n)
            point = solved / n
            assert lo <= point <= hi
            if solved > 0:
                assert lo > 0
            if solved < n:
                assert hi < 1

    def test_monotonic_in_successes(self):
        lows = [wilson_ci(s, 20)[0] for s in range(0, 21)]
        highs = [wilson_ci(s, 20)[1] for s in range(0, 21)]
        assert lows == sorted(lows)
        assert highs == sorted(highs)


class TestPairedBootstrap:
    def _paired_trials(self, solve_a: bool, solve_b: bool):
        trials_a = [
            make_trial(
                f"k{i}", "a", outcome=TrialOutcome.SOLVED if solve_a else TrialOutcome.UNSOLVED
            )
            for i in range(N_TRIALS)
        ]
        trials_b = [
            make_trial(
                f"k{i}", "b", outcome=TrialOutcome.SOLVED if solve_b else TrialOutcome.UNSOLVED
            )
            for i in range(N_TRIALS)
        ]
        return trials_a, trials_b

    def test_perfect_separation_is_conclusive(self):
        trials_a, trials_b = self._paired_trials(solve_a=True, solve_b=False)
        result = paired_bootstrap(trials_a, trials_b, n_boot=2000)
        assert result["n_pairs"] == N_TRIALS
        assert result["mean_diff_pp"] == pytest.approx(100.0)
        assert result["ci_lo_pp"] > 0
        assert result["conclusive"] is True

    def test_identical_trials_never_conclusive(self):
        trials_a, _ = self._paired_trials(solve_a=True, solve_b=True)
        result = paired_bootstrap(trials_a, trials_a, n_boot=2000)
        assert result["mean_diff_pp"] == 0.0
        assert result["ci_lo_pp"] == 0.0
        assert result["ci_hi_pp"] == 0.0
        assert result["conclusive"] is False

    def test_seeded_results_reproducible(self):
        trials_a, trials_b = self._paired_trials(solve_a=True, solve_b=False)
        r1 = paired_bootstrap(trials_a, trials_b, n_boot=500, seed=7)
        r2 = paired_bootstrap(trials_a, trials_b, n_boot=500, seed=7)
        assert r1 == r2

    def test_no_common_tasks_inconclusive(self):
        trials_a = [make_trial("x", "a")]
        trials_b = [make_trial("y", "b")]
        result = paired_bootstrap(trials_a, trials_b)
        assert result == {
            "n_pairs": 0,
            "mean_diff_pp": 0.0,
            "ci_lo_pp": 0.0,
            "ci_hi_pp": 0.0,
            "conclusive": False,
        }


def _metrics(target_id: str, rate: float, *, cost: float | None, total: float | None = None, p50: int = 1000):
    solved = round(rate * 10)
    lo, hi = wilson_ci(solved, 10)
    return TargetMetrics(
        target_id=target_id,
        n=10,
        solved=solved,
        solve_rate=rate,
        time_p50_ms=p50,
        time_p90_ms=p50 * 2,
        timeouts=0,
        errors=0,
        total_input_tokens=None,
        total_output_tokens=None,
        total_cost_usd=total,
        cost_source="HARNESS_REPORTED" if (cost is not None or total is not None) else None,
        cost_per_solve_usd=cost,
        effective_cost_usd=cost if cost is not None else total,
        mean_files_changed=None,
        wilson_lo=lo,
        wilson_hi=hi,
    )


class TestPareto:
    def test_dominated_target_excluded_on_quality_cost(self):
        metrics = [
            _metrics("A", 0.9, cost=2.0),
            _metrics("B", 0.8, cost=None, total=1.0),  # fallback to total cost
            _metrics("C", 0.8, cost=1.5),  # same quality as B, higher cost -> dominated
        ]
        result = pareto_frontier(metrics)
        assert result.axes == "quality-cost"
        assert set(result.frontier) == {"A", "B"}

    def test_cost_unavailable_switches_to_time(self):
        metrics = [
            _metrics("A", 0.9, cost=None, p50=500),
            _metrics("B", 0.8, cost=None, p50=300),
            _metrics("C", 0.8, cost=None, p50=400),  # dominated by B
        ]
        result = pareto_frontier(metrics)
        assert result.axes == "quality-time"
        assert set(result.frontier) == {"A", "B"}

    def test_single_unpriced_target_switches_to_time(self):
        # One unreported cost is enough: the cost axis needs EVERY target priced.
        metrics = [
            _metrics("A", 0.9, cost=2.0, p50=500),
            _metrics("B", 0.8, cost=None, total=None, p50=300),
        ]
        result = pareto_frontier(metrics)
        assert result.axes == "quality-time"
        assert set(result.frontier) == {"A", "B"}

    def test_empty_metrics(self):
        assert pareto_frontier([]).frontier == []


class TestRecommend:
    def test_cheapest_not_conclusively_worse_wins(self):
        metrics = {
            "claude": _metrics("claude", 0.9, cost=2.0, p50=60000),
            "codex": _metrics("codex", 0.85, cost=0.5, p50=50000),
            "glm": _metrics("glm", 0.7, cost=0.1),
        }
        comparisons = {
            ("claude", "codex"): {"conclusive": False},
            ("claude", "glm"): {"conclusive": True},
        }
        rec = recommend(metrics, comparisons)
        assert rec.best_quality_target == "claude"
        assert rec.candidates_not_worse == ["claude", "codex"]
        assert rec.recommended == "codex"

    def test_no_cost_data_no_recommendation(self):
        metrics = {
            "claude": _metrics("claude", 0.9, cost=None, p50=1000),
            "codex": _metrics("codex", 0.85, cost=None, p50=2000),
        }
        comparisons = {("claude", "codex"): {"conclusive": False}}
        rec = recommend(metrics, comparisons)
        assert rec.candidates_not_worse == ["claude", "codex"]
        assert rec.recommended is None
        assert "economic recommendation unavailable" in rec.reason.lower()

    def test_priced_vs_unpriced_disables_recommendation(self):
        # Honesty rule: one unpriced member of the not-conclusively-worse set
        # disables the economic recommendation — priced is never compared to unpriced.
        metrics = {
            "priced": _metrics("priced", 0.9, cost=1.0, p50=1000),
            "unpriced": _metrics("unpriced", 0.8, cost=None, total=None, p50=2000),
        }
        comparisons = {("priced", "unpriced"): {"conclusive": False}}
        rec = recommend(metrics, comparisons)
        assert rec.candidates_not_worse == ["priced", "unpriced"]
        assert rec.recommended is None
        assert "economic recommendation unavailable" in rec.reason.lower()

    def test_quality_tie_broken_by_time(self):
        metrics = {
            "slow": _metrics("slow", 0.5, cost=1.0, p50=2000),
            "fast": _metrics("fast", 0.5, cost=1.0, p50=500),
        }
        rec = recommend(metrics, {("fast", "slow"): {"conclusive": True}})
        assert rec.best_quality_target == "fast"

    def test_missing_comparison_counts_as_inconclusive(self):
        metrics = {
            "a": _metrics("a", 0.9, cost=1.0),
            "b": _metrics("b", 0.8, cost=0.1),
        }
        rec = recommend(metrics, {})  # no comparisons at all
        assert rec.candidates_not_worse == ["a", "b"]
        assert rec.recommended == "b"

    def test_empty_metrics(self):
        rec = recommend({}, {})
        assert rec.best_quality_target is None
        assert rec.recommended is None
