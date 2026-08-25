"""Unit tests for sampling, coverage, health, and statistics."""

from __future__ import annotations

from repobench.models import (
    RepoBenchConfig,
    CandidateTask,
    Complexity,
    TaskStatus,
    TaskType,
)
from repobench.benchmark.sampling import select_benchmark
from repobench.benchmark.coverage import calculate_coverage
from repobench.benchmark.health import calculate_health
from repobench.analysis.statistics import wilson_ci, paired_bootstrap_difference
from repobench.analysis.metrics import compute_config_metrics
from repobench.analysis.recommendation import recommend, pareto_frontier
from repobench.models import ConfigMetrics, Trial


def make_candidate(i: int, status: TaskStatus = TaskStatus.VALID) -> CandidateTask:
    task_type = TaskType.BUGFIX if i % 2 == 0 else TaskType.FEATURE
    subsystem = ["payments", "auth", "frontend"][i % 3]
    complexity = [Complexity.SMALL, Complexity.MEDIUM, Complexity.LARGE][i % 3]
    return CandidateTask(
        pr_number=100 + i,
        pr_title=f"PR {i}",
        status=status,
        task_type=task_type,
        subsystem=subsystem,
        complexity=complexity,
    )


class TestSampling:
    def test_selects_only_valid(self):
        candidates = [make_candidate(i) for i in range(10)]
        candidates[0].status = TaskStatus.REJECTED
        cfg = RepoBenchConfig()
        selected = select_benchmark(candidates, candidates, cfg)
        assert all(c.status == TaskStatus.VALID for c in selected)
        assert 0 < len(selected) <= 10

    def test_respects_size(self):
        candidates = [make_candidate(i) for i in range(30)]
        cfg = RepoBenchConfig(benchmark={"size": 5})
        selected = select_benchmark(candidates, candidates, cfg)
        assert len(selected) == 5

    def test_empty_pool(self):
        cfg = RepoBenchConfig()
        assert select_benchmark([], [], cfg) == []


class TestCoverage:
    def test_perfect_match(self):
        cov = calculate_coverage(
            {"task_type": {"bugfix": 0.5, "feature": 0.5}},
            {"task_type": {"bugfix": 0.5, "feature": 0.5}},
        )
        assert cov["task_type"] == 100.0

    def test_partial_match(self):
        cov = calculate_coverage(
            {"task_type": {"bugfix": 0.7, "feature": 0.3}},
            {"task_type": {"bugfix": 0.5, "feature": 0.5}},
        )
        assert 70 < cov["task_type"] < 100

    def test_empty_dimension(self):
        cov = calculate_coverage({}, {})
        assert cov == {}


class TestHealth:
    def test_health_components(self):
        tasks = [make_candidate(i) for i in range(12)]
        health = calculate_health(tasks, tasks)
        assert 0 <= health.overall <= 100
        assert 0 <= health.representativeness <= 100
        assert 0 <= health.validation <= 100
        assert 0 <= health.leakage <= 100

    def test_empty_health(self):
        health = calculate_health([])
        assert health.overall == 0


class TestStatistics:
    def test_wilson_ci_bounds(self):
        lo, hi = wilson_ci(21, 24)
        assert 0.0 < lo < hi < 1.0

    def test_wilson_ci_zero_total(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)

    def test_bootstrap_deterministic(self):
        a = [True] * 20 + [False] * 4
        b = [True] * 18 + [False] * 6
        d1 = paired_bootstrap_difference(a, b)
        d2 = paired_bootstrap_difference(a, b)
        assert d1 == d2  # deterministic seed

    def test_bootstrap_raises_on_length_mismatch(self):
        try:
            paired_bootstrap_difference([True], [True, False])
            assert False, "should raise"
        except ValueError:
            pass


class TestMetrics:
    def test_metrics_aggregation(self):
        trials = [
            Trial(task_id=f"t{i}", agent_config="a", solved=i < 8,
                  duration_ms=1000 + i, prompt_tokens=100, completion_tokens=50,
                  cost_usd=0.1)
            for i in range(10)
        ]
        m = compute_config_metrics(trials)
        assert m.solved == 8
        assert m.total == 10
        assert abs(m.pass_rate - 0.8) < 1e-9
        assert abs(m.cost_per_solve - 0.125) < 1e-9
        assert m.p50_duration_ms == 1004  # median of 1000..1009
        assert m.p90_duration_ms == 1008

    def test_metrics_empty(self):
        m = compute_config_metrics([])
        assert m.total == 0
        assert m.cost_per_solve is None


class TestRecommendation:
    def test_lowest_cost_when_indistinguishable(self):
        best = ConfigMetrics(solved=8, total=10, pass_rate=0.8,
                             ci_lower=0.5, ci_upper=0.95, cost_per_solve=1.0)
        cheap = ConfigMetrics(solved=7, total=10, pass_rate=0.7,
                              ci_lower=0.4, ci_upper=0.9, cost_per_solve=0.3)
        rec, reason = recommend({"best": best, "cheap": cheap})
        assert rec == "cheap"
        assert "cost" in reason

    def test_no_results(self):
        rec, reason = recommend({})
        assert rec is None

    def test_pareto_frontier(self):
        a = ConfigMetrics(solved=9, total=10, pass_rate=0.9, cost_per_solve=2.0)
        b = ConfigMetrics(solved=7, total=10, pass_rate=0.7, cost_per_solve=0.5)
        c = ConfigMetrics(solved=5, total=10, pass_rate=0.5, cost_per_solve=3.0)
        frontier = pareto_frontier({"a": a, "b": b, "c": c})
        assert "a" in frontier
        assert "b" in frontier
        assert "c" not in frontier  # dominated by both a and b
