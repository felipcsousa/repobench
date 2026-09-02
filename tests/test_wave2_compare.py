"""Wave 2 target regression across runs (issue #14, PRD §149): the
`repobench compare` builder over synthetic two-run storage, manifest
fingerprint warnings, CLI error paths and the same fast-forward e2e path as
the other wave suites (init → analyze → benchmark build → run with fake agents).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repobench.cli.reports import build_compare
from repobench.core.errors import UsageError
from repobench.core.paths import ProjectPaths
from repobench.core.types import (
    Assessment,
    TaskMetadata,
    TaskType,
    TrialOutcome,
    TrialResult,
)
from repobench.storage.db import Storage
from tests.test_e2e import _fast_forward, _invoke

BENCHMARK_ID = "rb_b_compare"


def _trial(
    run_id: str,
    task_id: str,
    target_id: str,
    outcome: TrialOutcome,
    *,
    trial_id: str | None = None,
    cost_usd: float | None = None,
) -> TrialResult:
    return TrialResult(
        trial_id=trial_id or f"trial_{run_id}_{task_id}_{target_id}",
        run_id=run_id,
        benchmark_id=BENCHMARK_ID,
        task_id=task_id,
        target_id=target_id,
        outcome=outcome,
        cost_usd=cost_usd,
        cost_source="USER_PROVIDED_PRICING" if cost_usd is not None else None,
    )


def _task_meta(task_id: str, *, subsystem: str, task_type: TaskType) -> TaskMetadata:
    return TaskMetadata(
        task_id=task_id,
        base_sha="b" * 40,
        gold_sha="g" * 40,
        assessment=Assessment(subsystem=subsystem, task_type=task_type),
    )


def _two_run_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "state.db")
    config_json = json.dumps({"bootstrap_seed": 42})
    storage.create_run("run_a", BENCHMARK_ID, config_json=config_json)
    storage.create_run("run_b", BENCHMARK_ID, config_json=config_json)
    return storage


def _write_manifest(
    root: Path, run_id: str, *, config_hash: str, harness_version: str | None
) -> None:
    """Fabricated wave-1 run manifest (runs/<id>/manifest.json)."""
    manifest = {
        "run_id": run_id,
        "benchmark_id": BENCHMARK_ID,
        "harnesses": {"command": harness_version},
        "targets": [
            {
                "definition": {"id": "glm", "harness": "command"},
                "config_hash": config_hash,
                "harness_version": harness_version,
            }
        ],
    }
    run_dir = ProjectPaths(root).run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(manifest))


# --------------------------------------------------------------- builder (unit)


class TestBuildCompare:
    def test_overall_delta_ci_and_cost_delta(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        for index in range(1, 6):
            task_id = f"task_{index}"
            storage.save_trial(
                _trial("run_a", task_id, "glm", TrialOutcome.UNSOLVED, cost_usd=0.10)
            )
            storage.save_trial(
                _trial("run_b", task_id, "glm", TrialOutcome.SOLVED, cost_usd=0.05)
            )

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")

        assert (outcome.run_a, outcome.run_b) == ("run_a", "run_b")
        assert outcome.benchmark_id == BENCHMARK_ID
        assert (outcome.tasks_only_a, outcome.tasks_only_b) == (0, 0)
        assert len(outcome.targets) == 1
        delta = outcome.targets[0]
        assert delta.target_id == "glm"
        assert (delta.n_tasks_a, delta.n_tasks_b, delta.common_tasks) == (5, 5, 5)
        assert delta.rate_a == 0.0 and delta.rate_b == 1.0
        # every resample keeps the same 5 solved vs 0: degenerate +100pp CI
        assert delta.diff_pp == pytest.approx(100.0)
        assert delta.ci_lo_pp == pytest.approx(100.0)
        assert delta.ci_hi_pp == pytest.approx(100.0)
        assert delta.conclusive is True
        # A: $0.50 total / 0 solves; B: $0.25 total -> cost per solve $0.05
        assert delta.cost_a == pytest.approx(0.50)
        assert delta.cost_b == pytest.approx(0.05)
        assert delta.cost_delta_pct == pytest.approx(-90.0)

    def test_retry_attempts_dedupe_to_last_verdict(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        # issue #13: a retry leaves two stored attempts for one (task, target)
        storage.save_trial(
            _trial(
                "run_a", "task_1", "glm", TrialOutcome.UNSOLVED, trial_id="trial_a1"
            )
        )
        storage.save_trial(
            _trial("run_a", "task_1", "glm", TrialOutcome.SOLVED, trial_id="trial_a2")
        )
        # pin the first attempt in the past so created_at ordering is explicit
        storage.execute(
            "UPDATE trials SET created_at = '2026-01-01T00:00:00+00:00' "
            "WHERE trial_id = 'trial_a1'"
        )
        storage.save_trial(_trial("run_b", "task_1", "glm", TrialOutcome.SOLVED))

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")
        delta = outcome.targets[0]
        # one trial per task despite two stored attempts — the last wins
        assert (delta.n_tasks_a, delta.n_tasks_b, delta.common_tasks) == (1, 1, 1)
        assert delta.rate_a == 1.0

    def test_segments_pool_all_targets_and_drop_one_sided_segments(
        self, tmp_path: Path
    ) -> None:
        storage = _two_run_storage(tmp_path)
        storage.save_task(
            "t_pay",
            data=_task_meta(
                "t_pay", subsystem="payments", task_type=TaskType.BUGFIX
            ).model_dump(mode="json"),
        )
        storage.save_task(
            "t_fe",
            data=_task_meta(
                "t_fe", subsystem="frontend", task_type=TaskType.FEATURE
            ).model_dump(mode="json"),
        )
        storage.save_task(
            "t_a_only",
            data=_task_meta(
                "t_a_only", subsystem="billing", task_type=TaskType.REFACTOR
            ).model_dump(mode="json"),
        )

        # two targets per run: segment rates must pool both targets of a run
        storage.save_trial(_trial("run_a", "t_pay", "glm", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_a", "t_pay", "other", TrialOutcome.UNSOLVED))
        storage.save_trial(_trial("run_a", "t_fe", "glm", TrialOutcome.UNSOLVED))
        storage.save_trial(_trial("run_a", "t_fe", "other", TrialOutcome.UNSOLVED))
        storage.save_trial(_trial("run_a", "t_a_only", "glm", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_b", "t_pay", "glm", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_b", "t_pay", "other", TrialOutcome.UNSOLVED))
        storage.save_trial(_trial("run_b", "t_fe", "glm", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_b", "t_fe", "other", TrialOutcome.SOLVED))

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")

        assert (outcome.tasks_only_a, outcome.tasks_only_b) == (1, 0)
        assert any("1 tasks in A missing from B" in w for w in outcome.warnings)

        subsystem = {d.segment: d for d in outcome.segments["subsystem"]}
        assert set(subsystem) == {"payments", "frontend"}  # billing is A-only
        assert (subsystem["payments"].n_a, subsystem["payments"].n_b) == (2, 2)
        assert subsystem["payments"].rate_a == pytest.approx(0.5)  # pooled 1/2
        assert subsystem["payments"].rate_b == pytest.approx(0.5)
        assert subsystem["payments"].diff_pp == 0.0
        assert subsystem["frontend"].rate_a == 0.0
        assert subsystem["frontend"].rate_b == pytest.approx(1.0)  # pooled 2/2
        assert subsystem["frontend"].diff_pp == pytest.approx(100.0)

        task_type = {d.segment: d for d in outcome.segments["task_type"]}
        assert set(task_type) == {"bugfix", "feature"}  # refactor is A-only

    def test_target_present_in_one_run_only_is_honest_zero(
        self, tmp_path: Path
    ) -> None:
        storage = _two_run_storage(tmp_path)
        storage.save_trial(_trial("run_a", "task_1", "fixer", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_b", "task_1", "noop", TrialOutcome.UNSOLVED))

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")
        by_target = {delta.target_id: delta for delta in outcome.targets}
        # fixer never ran in B: rate 0.0, no common tasks, CI skipped
        fixer = by_target["fixer"]
        assert (fixer.n_tasks_a, fixer.n_tasks_b, fixer.common_tasks) == (1, 0, 0)
        assert fixer.rate_a == 1.0 and fixer.rate_b == 0.0
        assert fixer.diff_pp == 0.0 and fixer.conclusive is False
        assert by_target["noop"].rate_b == 0.0


# ----------------------------------------------------- fingerprint warnings (#14)


class TestFingerprintWarnings:
    def _seed_trials(self, storage: Storage) -> None:
        storage.save_trial(_trial("run_a", "task_1", "glm", TrialOutcome.SOLVED))
        storage.save_trial(_trial("run_b", "task_1", "glm", TrialOutcome.SOLVED))

    def test_matching_manifests_warn_nothing(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        self._seed_trials(storage)
        _write_manifest(tmp_path, "run_a", config_hash="sha256:aaa", harness_version="1.0")
        _write_manifest(tmp_path, "run_b", config_hash="sha256:aaa", harness_version="1.0")

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")
        assert outcome.warnings == []

    def test_config_hash_and_harness_version_changes_warn(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        self._seed_trials(storage)
        _write_manifest(tmp_path, "run_a", config_hash="sha256:aaa", harness_version="1.0")
        _write_manifest(tmp_path, "run_b", config_hash="sha256:bbb", harness_version="2.0")

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")
        assert any(
            "target glm config changed between runs — results are not directly comparable"
            in warning
            for warning in outcome.warnings
        )
        assert any(
            "harness command version changed between runs (1.0 → 2.0)" in warning
            for warning in outcome.warnings
        )

    def test_missing_manifests_warn_instead_of_crashing(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        self._seed_trials(storage)

        outcome = build_compare(tmp_path, storage, "run_a", "run_b")
        assert sum(
            "has no reproducibility manifest — fingerprint comparison unavailable"
            in warning
            for warning in outcome.warnings
        ) == 2


# ------------------------------------------------------------- builder errors


class TestBuildCompareErrors:
    def test_different_benchmarks_are_rejected(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "state.db")
        storage.create_run("run_x", "rb_b_one")
        storage.create_run("run_y", "rb_b_two")

        with pytest.raises(UsageError) as excinfo:
            build_compare(tmp_path, storage, "run_x", "run_y")
        message = str(excinfo.value)
        assert "same benchmark" in message
        assert "rb_b_one" in message and "rb_b_two" in message

    def test_unknown_run_is_rejected(self, tmp_path: Path) -> None:
        storage = _two_run_storage(tmp_path)
        with pytest.raises(UsageError) as excinfo:
            build_compare(tmp_path, storage, "run_nope", "run_a")
        assert "unknown run: run_nope" in str(excinfo.value)


# ----------------------------------------------------------------- CLI wiring


class TestCompareCli:
    def test_cli_rejects_mismatched_benchmarks_and_unknown_runs(
        self, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fixture_repo)
        assert _invoke("init", "--yes").exit_code == 0
        storage = Storage(fixture_repo / ".repobench" / "state.db")
        storage.create_run("run_x", "rb_b_one")
        storage.create_run("run_y", "rb_b_two")

        mismatched = _invoke("compare", "run_x", "run_y")
        assert mismatched.exit_code == 1
        assert "same benchmark" in mismatched.output

        unknown = _invoke("compare", "run_nope", "run_x")
        assert unknown.exit_code == 1
        assert "unknown run: run_nope" in unknown.output

    def test_unknown_format_is_rejected(
        self, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fixture_repo)
        assert _invoke("init", "--yes").exit_code == 0
        result = _invoke("compare", "run_a", "run_b", "--format", "html")
        assert result.exit_code == 1
        assert "unknown compare format" in result.output


# ------------------------------------------------------------------ e2e (lite)


class TestCompareEndToEnd:
    def test_compare_two_runs_text_and_json(
        self,
        fixture_repo: Path,
        fake_agent_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        # run 1 (baseline): both targets
        first = _invoke("run", "fixer", "noop", "--yes", "--trust-custom-command")
        assert first.exit_code == 0, first.output
        # run 2: only noop — a fresh run (no --resume)
        second = _invoke("run", "noop", "--yes", "--trust-custom-command")
        assert second.exit_code == 0, second.output

        runs = storage.list_runs()  # newest first
        assert len(runs) == 2
        run_b, run_a = runs[0]["run_id"], runs[1]["run_id"]
        listing = _invoke("runs")
        assert listing.exit_code == 0, listing.output
        assert run_a in listing.output and run_b in listing.output

        text = _invoke("compare", run_a, run_b)
        assert text.exit_code == 0, text.output
        assert f"{run_a} → {run_b}" in text.output
        assert "Overall" in text.output
        # fixer solved in A, absent from B — honest 100% → 0%
        assert "fixer  100% → 0%" in text.output
        assert "noop  0% → 0%" in text.output
        assert "Cost" in text.output
        assert "Segments — subsystem" in text.output

        as_json = _invoke("compare", run_a, run_b, "--format", "json")
        assert as_json.exit_code == 0, as_json.output
        data = json.loads(as_json.output)
        assert data["run_a"] == run_a and data["run_b"] == run_b
        assert data["benchmark_id"] == runs[0]["benchmark_id"]
        rates = {t["target_id"]: (t["rate_a"], t["rate_b"]) for t in data["targets"]}
        assert rates["fixer"] == (1.0, 0.0)
        assert rates["noop"] == (0.0, 0.0)
        # both runs share the single benchmark task and the same manifests
        assert data["tasks_only_a"] == 0 and data["tasks_only_b"] == 0
        assert data["warnings"] == []
