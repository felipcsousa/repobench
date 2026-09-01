"""Benchmark module tests: greedy sampling, coverage, health, manifests (PRD §83-89)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from repobench.benchmark import (
    CoverageReport,
    build_manifest,
    compute_health,
    coverage_report,
    greedy_stratified_sample,
    load_manifest,
    save_manifest,
)
from repobench.config import BenchmarkConfig, BenchmarkDimensions
from repobench.core.ids import METHODOLOGY_VERSION
from repobench.core.types import (
    Assessment,
    Complexity,
    TaskMetadata,
    TaskType,
    WorkloadDistribution,
)

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def make_task(
    task_id: str,
    *,
    task_type: TaskType = TaskType.BUGFIX,
    subsystem: str = "core",
    complexity: Complexity = Complexity.MEDIUM,
    version: int = 1,
    created_at: datetime | None = None,
) -> TaskMetadata:
    return TaskMetadata(
        task_id=task_id,
        base_sha="b" * 40,
        gold_sha="g" * 40,
        assessment=Assessment(
            task_type=task_type, subsystem=subsystem, complexity=complexity
        ),
        version=version,
        created_at=created_at,
    )


def bugfix_heavy_universe() -> list[TaskMetadata]:
    """5 bugfixes in subsystem 'core' + 1 feature alone in subsystem 'api'."""
    tasks = [make_task(f"t{i}", subsystem="core") for i in range(5)]
    tasks.append(make_task("feat", task_type=TaskType.FEATURE, subsystem="api"))
    return tasks


class TestSampling:
    def test_balanced_universe_size2_picks_one_of_each_type(self):
        # 2 bugfix/core/small vs 2 feature/api/large: adding the second bugfix to the
        # first leaves every dimension at TV 0.5, while adding a feature hits TV 0.
        tasks = [
            make_task("t0"),
            make_task("t1"),
            make_task("t2", task_type=TaskType.FEATURE, subsystem="api", complexity=Complexity.LARGE),
            make_task("t3", task_type=TaskType.FEATURE, subsystem="api", complexity=Complexity.LARGE),
        ]
        sample = greedy_stratified_sample(tasks, 2, BenchmarkDimensions())
        types = {t.assessment.task_type for t in sample}
        assert len(sample) == 2
        assert types == {TaskType.BUGFIX, TaskType.FEATURE}

    def test_minority_type_included_once_sample_share_can_approach_universe(self):
        # With universe feature share 1/6, a size-2 sample holding the feature measures
        # share 1/2 (TV 1/3) versus 1/6 when excluded, so TV minimization prefers two
        # bugfixes at size 2. At size 4 the feature measures 1/4 (TV 1/12) — strictly
        # closer than exclusion — so greedy must pick it.
        tasks = bugfix_heavy_universe()
        sample4 = greedy_stratified_sample(tasks, 4, BenchmarkDimensions())
        assert len(sample4) == 4
        assert sum(1 for t in sample4 if t.assessment.task_type == TaskType.FEATURE) == 1
        assert sum(1 for t in sample4 if t.assessment.task_type == TaskType.BUGFIX) == 3
        # And the small-sample behavior is still distance-optimal: pure bugfixes.
        sample2 = greedy_stratified_sample(tasks, 2, BenchmarkDimensions())
        assert all(t.assessment.task_type == TaskType.BUGFIX for t in sample2)

    def test_size_zero_and_oversize(self):
        tasks = bugfix_heavy_universe()
        assert greedy_stratified_sample(tasks, 0, BenchmarkDimensions()) == []
        assert greedy_stratified_sample(tasks, -3, BenchmarkDimensions()) == []
        assert greedy_stratified_sample(tasks, len(tasks), BenchmarkDimensions()) == tasks
        assert greedy_stratified_sample(tasks, 100, BenchmarkDimensions()) == tasks

    def test_deterministic_across_calls(self):
        # Greedy tie-breaks by task_id, so repeated calls always agree — no seed involved.
        tasks = bugfix_heavy_universe()
        a = greedy_stratified_sample(tasks, 3, BenchmarkDimensions())
        b = greedy_stratified_sample(tasks, 3, BenchmarkDimensions())
        assert [t.task_id for t in a] == [t.task_id for t in b]


class TestCoverage:
    def test_identical_distributions_score_100(self):
        universe = WorkloadDistribution(
            task_type={"bugfix": 1.0},
            subsystem={"core": 1.0},
            complexity={"medium": 1.0},
        )
        report = coverage_report(universe, [make_task("x")], BenchmarkDimensions())
        assert report == CoverageReport(task_type=100, subsystem=100, complexity=100, overall=100)

    def test_disjoint_distributions_score_0(self):
        universe = WorkloadDistribution(
            task_type={"bugfix": 1.0},
            subsystem={"api": 1.0},
            complexity={"large": 1.0},
        )
        sample = [make_task("x", task_type=TaskType.FEATURE, subsystem="core", complexity=Complexity.SMALL)]
        report = coverage_report(universe, sample, BenchmarkDimensions())
        assert report == CoverageReport(task_type=0, subsystem=0, complexity=0, overall=0)

    def test_partial_overlap_hand_computed(self):
        # task_type: sample bugfix=1 vs universe {bugfix .5, feature .5} -> TV 0.5 -> 50.
        # subsystem/complexity identical -> 100. Overall = 0.3*50 + 0.4*100 + 0.3*100 = 85.
        universe = WorkloadDistribution(
            task_type={"bugfix": 0.5, "feature": 0.5},
            subsystem={"core": 1.0},
            complexity={"medium": 1.0},
        )
        report = coverage_report(universe, [make_task("x")], BenchmarkDimensions())
        assert report.task_type == 50
        assert report.subsystem == 100
        assert report.complexity == 100
        assert report.overall == 85


class TestHealth:
    def test_overall_known_inputs(self):
        # 40/25/15/10/10 -> round(32 + 22.5 + 10.5 + 5 + 10) = 80.
        coverage = CoverageReport(task_type=80, subsystem=80, complexity=80, overall=80)
        tasks = [
            make_task("a", subsystem="core", task_type=TaskType.BUGFIX, created_at=NOW - timedelta(days=90)),
            make_task("b", subsystem="api", task_type=TaskType.FEATURE, created_at=NOW - timedelta(days=90)),
        ]
        health = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=0.9,
            leakage_score=70,
            tasks=tasks,
            lookback_days=180,
            now=NOW,
        )
        assert health.representativeness == 80
        assert health.validation_confidence == 90
        assert health.leakage_resistance == 70
        assert health.recency == 50  # median age 90 of lookback 180
        assert health.diversity == 100  # 2/2 subsystems, 2/2 types
        assert health.overall == 80

    def test_network_warning_always_present(self):
        coverage = CoverageReport(task_type=100, subsystem=100, complexity=100, overall=100)
        health = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=100,
            tasks=[],
            now=NOW,
        )
        assert "No network isolation (host-native execution)" in health.warnings
        assert health.recency == 50  # no timestamps -> neutral
        assert health.diversity == 0

    def test_underrepresented_task_type_warning(self):
        coverage = CoverageReport(task_type=90, subsystem=90, complexity=90, overall=90)
        # 8 bugfix + 2 feature matches universe shares {0.8, 0.2} exactly.
        matched = [make_task(f"t{i}") for i in range(8)]
        matched += [
            make_task(f"f{i}", task_type=TaskType.FEATURE, subsystem="api") for i in range(2)
        ]
        balanced = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=90,
            tasks=matched,
            universe_counts={"bugfix": 8, "feature": 2},
            now=NOW,
        )
        assert not any("underrepresented" in w for w in balanced.warnings)
        sample = [make_task(f"t{i}") for i in range(10)]  # all bugfix
        skewed = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=90,
            tasks=sample,
            universe_counts={"feature": 2, "bugfix": 8},
            now=NOW,
        )
        assert "feature work underrepresented" in skewed.warnings


class TestManifest:
    def test_id_shape_and_stability(self):
        tasks = bugfix_heavy_universe()[:3]
        config = BenchmarkConfig(size=3)
        manifest1 = build_manifest(tasks, None, None, config, repository="acme/payments")
        manifest2 = build_manifest(tasks, None, None, config, repository="acme/payments")
        assert manifest1.benchmark_id.startswith("rb_b_")
        assert manifest1.benchmark_id == manifest2.benchmark_id
        assert manifest1.methodology_version == METHODOLOGY_VERSION
        assert manifest1.size == 3
        assert manifest1.task_versions == {t.task_id: 1 for t in tasks}
        assert manifest1.config_snapshot["size"] == 3

    def test_id_changes_with_task_versions_or_config(self):
        tasks = bugfix_heavy_universe()[:3]
        config = BenchmarkConfig(size=3)
        base = build_manifest(tasks, None, None, config)
        bumped = [t.model_copy(update={"version": 2}) if t.task_id == "t0" else t for t in tasks]
        assert build_manifest(bumped, None, None, config).benchmark_id != base.benchmark_id
        assert build_manifest(tasks, None, None, BenchmarkConfig(size=4)).benchmark_id != base.benchmark_id

    def test_save_load_roundtrip(self, tmp_path):
        tasks = bugfix_heavy_universe()[:2]
        coverage = CoverageReport(task_type=90, subsystem=80, complexity=95, overall=88)
        health = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=78,
            tasks=tasks,
            now=NOW,
        )
        manifest = build_manifest(tasks, health, coverage, BenchmarkConfig(size=2), repository="acme/payments")
        path = save_manifest(manifest, tmp_path / "benchmarks" / "abc")
        assert path.name == "manifest.json"
        loaded = load_manifest(tmp_path / "benchmarks" / "abc")
        assert loaded == manifest
