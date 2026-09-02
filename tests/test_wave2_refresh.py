"""Wave 2 benchmark drift detection (issue #15, PRD §148): the pure
`compute_drift` rules, the `benchmark refresh` e2e path (re-analyze → drift →
rebuild, with the old benchmark untouched), reuse through refresh, and the
polite error / missing-task accounting paths."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from repobench.benchmark.coverage import CoverageReport
from repobench.benchmark.drift import compute_drift
from repobench.cli.builds import refresh_benchmark
from repobench.config import RepoBenchConfig
from repobench.core.paths import ProjectPaths
from repobench.core.types import (
    Assessment,
    Complexity,
    TaskMetadata,
    TaskType,
    WorkloadDistribution,
)
from repobench.storage.db import Storage
from tests.fixtures.gitutil import commit_all, make_pr
from tests.test_e2e import _fast_forward, _invoke
from tests.test_wave2_incremental import REUSED_CHECK_NAME, _validation_rows

# PR #9 content, shaped like PR #7: a buggy `utils.py` is seeded on main first
# (a plain commit — mining only sees merge commits), then PR #9 fixes slugify
# and extends the module. The hidden test imports only names that exist on the
# buggy base, so the noop check fails with exit 1 (a real verifier failure)
# instead of a collection error (exit 2 = inconclusive environment problem).
UTILS_BUGGY = '''"""Small string helpers seeded on main before PR #9."""


def slugify(text):
    """Lowercase text and join whitespace-separated words with dashes."""
    words = []
    for word in text.split():
        cleaned = "".join(char for char in word.lower() if char.isalnum())
        if cleaned:
            words.append(cleaned)
    return "_".join(words)  # bug: joins with underscores


def truncate(text, limit):
    """Cut text to limit characters without cutting the last word short."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " not in cut:
        return cut
    return cut.rsplit(" ", 1)[0] + "..."
'''

# The PR diff: docstring rewrite + join fix + a new `initials` helper —
# ≥ min_implementation_loc (20) added+removed implementation lines.
UTILS_FIXED = '''"""Small string helpers used across the calculator project.

The helpers are deliberately tiny: they exist so the fixture has a second
implementation module a merged PR can evolve.
"""


def slugify(text):
    """Lowercase text and join whitespace-separated words with dashes."""
    words = []
    for word in text.split():
        cleaned = "".join(char for char in word.lower() if char.isalnum())
        if cleaned:
            words.append(cleaned)
    return "-".join(words)


def truncate(text, limit):
    """Cut text to limit characters without cutting the last word short."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " not in cut:
        return cut
    return cut.rsplit(" ", 1)[0] + "..."


def initials(text, limit=3):
    """First letters of the words in text, uppercased, capped at limit."""
    letters = []
    stripped = text.strip()
    for word in stripped.split():
        cleaned = "".join(char for char in word.lower() if char.isalnum())
        if not cleaned:
            continue
        letters.append(cleaned[0])
        if len(letters) >= limit:
            break
    return "".join(letters).upper()
'''

TEST_UTILS = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import slugify, truncate


def test_slugify_joins_words_with_dashes():
    assert slugify("Hello RepoBench World") == "hello-repobench-world"


def test_truncate_keeps_last_word_whole():
    assert truncate("one two three", 7) == "one..."
'''


def _seed_and_merge_utils_pr(fixture_repo: Path) -> None:
    """Extend the fixture repo so a second good candidate exists (PR #9)."""
    (fixture_repo / "utils.py").write_text(UTILS_BUGGY)
    commit_all(fixture_repo, "seed utils module")
    make_pr(
        fixture_repo,
        9,
        "feat/utils",
        {"utils.py": UTILS_FIXED, "tests/test_utils.py": TEST_UTILS},
        "fix slugify separator and add initials helper",
    )


def _task(
    task_id: str, task_type: TaskType, subsystem: str, complexity: Complexity
) -> TaskMetadata:
    return TaskMetadata(
        task_id=task_id,
        base_sha="b" * 40,
        gold_sha="g" * 40,
        assessment=Assessment(
            task_type=task_type, subsystem=subsystem, complexity=complexity
        ),
    )


# ------------------------------------------------------- #15 compute_drift rules


class TestComputeDrift:
    def test_any_overall_drop_drifts_and_names_the_right_segment(self) -> None:
        before = CoverageReport(task_type=90, subsystem=90, complexity=90, overall=90)
        after = CoverageReport(task_type=60, subsystem=85, complexity=88, overall=77)
        universe = WorkloadDistribution(
            task_type={"bugfix": 0.75, "integration": 0.25},
            subsystem={"api": 1.0},
            complexity={"small": 1.0},
        )
        sample = [
            _task("t1", TaskType.BUGFIX, "api", Complexity.SMALL),
            _task("t2", TaskType.BUGFIX, "api", Complexity.SMALL),
        ]
        report = compute_drift(before, after, universe, sample)
        assert report.drifted is True  # any drop counts — magnitude lives in the numbers
        assert report.overall_before == 90
        assert report.overall_after == 77
        assert report.per_dimension == {
            "task_type": (90, 60),
            "subsystem": (90, 85),
            "complexity": (90, 88),
        }
        # task_type dropped the most (−30); integration is 25% of the universe
        # and absent from the sample.
        assert report.reasons == [
            "integration work increased and is underrepresented in the benchmark"
        ]

    def test_rise_or_hold_is_not_drifted_and_has_no_reasons(self) -> None:
        before = CoverageReport(task_type=70, subsystem=80, complexity=60, overall=70)
        universe = WorkloadDistribution(task_type={"bugfix": 1.0})
        sample = [_task("t1", TaskType.BUGFIX, "api", Complexity.SMALL)]
        rise = compute_drift(
            before,
            CoverageReport(task_type=80, subsystem=80, complexity=60, overall=74),
            universe,
            sample,
        )
        hold = compute_drift(
            before,
            CoverageReport(task_type=70, subsystem=80, complexity=60, overall=70),
            universe,
            sample,
        )
        assert rise.drifted is False and rise.reasons == []
        assert hold.drifted is False and hold.reasons == []

    def test_no_reason_when_no_segment_is_underrepresented(self) -> None:
        before = CoverageReport(task_type=80, subsystem=90, complexity=90, overall=86)
        after = CoverageReport(task_type=50, subsystem=89, complexity=89, overall=76)
        universe = WorkloadDistribution(task_type={"bugfix": 1.0})
        sample = [_task("t1", TaskType.BUGFIX, "api", Complexity.SMALL)]
        report = compute_drift(before, after, universe, sample)
        assert report.drifted is True
        assert report.reasons == []  # bugfix: universe 1.0 == sample 1.0 — honest: no story

    def test_at_most_two_reasons_ranked_by_margin(self) -> None:
        before = CoverageReport(task_type=90, subsystem=90, complexity=90, overall=90)
        after = CoverageReport(task_type=20, subsystem=90, complexity=90, overall=43)
        universe = WorkloadDistribution(
            task_type={"integration": 0.5, "feature": 0.3, "bugfix": 0.2}
        )
        sample = [_task("t1", TaskType.BUGFIX, "api", Complexity.SMALL)]
        report = compute_drift(before, after, universe, sample)
        assert report.reasons == [
            "integration work increased and is underrepresented in the benchmark",
            "feature work increased and is underrepresented in the benchmark",
        ]


# ------------------------------------------------------------ #15 e2e behavior


class TestRefreshCommand:
    def test_refresh_after_a_new_pr_builds_a_new_benchmark(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        old_row = storage.list_benchmarks()[0]
        old_id = old_row["benchmark_id"]
        old_manifest_path = Path(old_row["manifest_path"])
        old_manifest_bytes = old_manifest_path.read_bytes()
        old_task_ids = set(storage.benchmark_task_ids(old_id))

        # The repo evolved: a second good candidate exists after PR #9 merges.
        _seed_and_merge_utils_pr(fixture_repo)

        result = _invoke("benchmark", "refresh", "--size", "2")
        assert result.exit_code == 0, result.output
        assert "Benchmark refresh" in result.output
        assert old_id in result.output
        assert "Coverage:" in result.output
        assert "1 new" in result.output

        benchmarks = storage.list_benchmarks()
        new_id = benchmarks[0]["benchmark_id"]
        assert new_id != old_id  # a rebuild produces a NEW benchmark (PRD §89)
        assert old_id in {row["benchmark_id"] for row in benchmarks}
        new_task_ids = storage.benchmark_task_ids(new_id)
        assert len(new_task_ids) == 2
        assert set(new_task_ids) - old_task_ids  # the new PR's task shipped
        # Immutability: the old benchmark row's manifest is byte-for-byte untouched.
        assert old_manifest_path.read_bytes() == old_manifest_bytes

    def test_refresh_reuse_valid_skips_revalidation(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_id = storage.benchmark_task_ids(storage.list_benchmarks()[0]["benchmark_id"])[0]
        determinism_before = len(_validation_rows(storage, task_id, "determinism"))
        assert determinism_before >= 1  # sanity: the first build really validated

        result = _invoke("benchmark", "refresh", "--reuse-valid")
        assert result.exit_code == 0, result.output
        assert "Reused valid tasks" in result.output
        # reuse works through refresh: no determinism revalidation happened...
        assert len(_validation_rows(storage, task_id, "determinism")) == determinism_before
        # ...and the reuse event is recorded once in the append-only log
        assert len(_validation_rows(storage, task_id, REUSED_CHECK_NAME)) == 1

    def test_unknown_benchmark_id_fails(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        result = _invoke("benchmark", "refresh", "--benchmark", "rb_b_nope")
        assert result.exit_code == 1
        assert "unknown benchmark" in result.output

    def test_missing_manifest_file_fails_politely(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        storage = Storage(fixture_repo / ".repobench" / "state.db")
        Path(storage.list_benchmarks()[0]["manifest_path"]).unlink()
        result = _invoke("benchmark", "refresh")
        assert result.exit_code == 1
        assert "no loadable manifest" in result.output


# ------------------------------------------------- #15 service-level accounting


class TestRefreshService:
    def test_missing_old_sample_tasks_are_counted_not_fatal(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fixture_repo)
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_id = storage.benchmark_task_ids(storage.list_benchmarks()[0]["benchmark_id"])[0]
        # The old sample's task is gone from disk AND from the tasks table.
        shutil.rmtree(ProjectPaths(fixture_repo).task_dir(task_id))
        storage.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

        cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
        outcome = refresh_benchmark(fixture_repo, cfg, storage)
        assert outcome.missing_tasks == 1
        assert outcome.new_benchmark_id  # the refresh still succeeded
