"""Wave 2 multi-rollout reliability (issue #13): pass@k / pass^k estimators,
trials.rollout persistence + migration, rollout-aware run planning and the
reliability report section — unit tests plus the same fast-forward e2e path as
the wave-1 suite (init → analyze → benchmark build → run with fake agents).
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from repobench.analysis.reliability import TargetReliability, reliability_stats
from repobench.config import RepoBenchConfig
from repobench.core.errors import UsageError
from repobench.core.types import ExecutionTarget, TrialOutcome, TrialResult
from repobench.execution.runner import run_matrix
from repobench.storage.db import Storage
from tests.test_e2e import _command_target, _fast_forward, _invoke


def _trial(
    task_id: str,
    target_id: str,
    outcome: TrialOutcome,
    *,
    rollout: int = 1,
    cost_usd: float | None = None,
) -> TrialResult:
    return TrialResult(
        trial_id=f"trial_{task_id}_{target_id}_{rollout}_{outcome.value}",
        task_id=task_id,
        target_id=target_id,
        rollout=rollout,
        outcome=outcome,
        cost_usd=cost_usd,
        cost_source="USER_PROVIDED_PRICING" if cost_usd is not None else None,
    )


# ------------------------------------------------------- #13 reliability math


class TestReliabilityStats:
    def test_pass_at_k_unbiased_estimator_closed_form(self) -> None:
        # n=10, c=3, k=5 -> 1 - C(7,5)/C(10,5) = 1 - 21/252
        trials = [_trial("t1", "glm", TrialOutcome.SOLVED, rollout=i) for i in range(1, 4)]
        trials += [
            _trial("t1", "glm", TrialOutcome.UNSOLVED, rollout=i) for i in range(4, 11)
        ]
        stats = reliability_stats(trials, k=5)["glm"]
        assert stats.pass_at_k == pytest.approx(1 - math.comb(7, 5) / math.comb(10, 5))
        assert stats.n_tasks == 1

    def test_pass_at_k_full_solve_is_one_and_zero_solve_is_zero(self) -> None:
        solved = [_trial("t1", "full", TrialOutcome.SOLVED, rollout=i) for i in range(1, 6)]
        failed = [_trial("t1", "zero", TrialOutcome.UNSOLVED, rollout=i) for i in range(1, 6)]
        stats = reliability_stats(solved + failed, k=5)
        assert stats["full"].pass_at_k == 1.0  # C(0,5)/C(5,5) = 0
        assert stats["zero"].pass_at_k == 0.0  # C(5,5)/C(5,5) = 1

    def test_pass_hat_counts_only_fully_solved_tasks(self) -> None:
        trials = [
            _trial("all_solved", "glm", TrialOutcome.SOLVED, rollout=1),
            _trial("all_solved", "glm", TrialOutcome.SOLVED, rollout=2),
            _trial("half_lucky", "glm", TrialOutcome.SOLVED, rollout=1),
            _trial("half_lucky", "glm", TrialOutcome.UNSOLVED, rollout=2),
        ]
        stats = reliability_stats(trials, k=2)["glm"]
        assert stats.all_solved_tasks == 1
        assert stats.pass_hat_k == 0.5

    def test_pass_at_k_averages_per_task_estimator_and_variance(self) -> None:
        # task_a: 3/3 solved; task_b: 1/3 solved; k=2
        # task_a pass@2 = 1.0; task_b pass@2 = 1 - C(2,2)/C(3,2) = 2/3
        trials = [
            _trial("task_a", "glm", TrialOutcome.SOLVED, rollout=i) for i in (1, 2, 3)
        ]
        trials += [
            _trial("task_b", "glm", TrialOutcome.SOLVED, rollout=1),
            _trial("task_b", "glm", TrialOutcome.UNSOLVED, rollout=2),
            _trial("task_b", "glm", TrialOutcome.UNSOLVED, rollout=3),
        ]
        stats = reliability_stats(trials, k=2)["glm"]
        assert stats.pass_at_k == pytest.approx((1.0 + 2 / 3) / 2)
        assert stats.pass_hat_k == 0.5
        assert stats.all_solved_tasks == 1
        # per-task rates [1.0, 1/3]: population variance around the 2/3 mean
        assert stats.per_task_variance == pytest.approx(1 / 9)

    def test_tasks_with_fewer_rollouts_than_k_are_skipped(self) -> None:
        # never invent data: task_b has a single rollout and cannot enter a k=2
        # estimate; a target whose every task is short ends up with zero tasks.
        trials = [
            _trial("task_a", "rich", TrialOutcome.SOLVED, rollout=1),
            _trial("task_a", "rich", TrialOutcome.SOLVED, rollout=2),
            _trial("task_b", "rich", TrialOutcome.UNSOLVED, rollout=1),
            _trial("task_a", "poor", TrialOutcome.SOLVED, rollout=1),
        ]
        stats = reliability_stats(trials, k=2)
        assert stats["rich"].n_tasks == 1
        assert stats["rich"].pass_at_k == 1.0  # only task_a counts
        assert stats["poor"].n_tasks == 0

    def test_single_task_variance_is_zero(self) -> None:
        trials = [_trial("only", "glm", TrialOutcome.SOLVED, rollout=1)]
        assert reliability_stats(trials, k=1)["glm"].per_task_variance == 0.0

    def test_k1_with_one_rollout_matches_aggregate_solve_rate(self) -> None:
        trials = [
            _trial("t1", "glm", TrialOutcome.SOLVED),
            _trial("t2", "glm", TrialOutcome.SOLVED),
            _trial("t3", "glm", TrialOutcome.UNSOLVED),
            _trial("t4", "glm", TrialOutcome.UNSOLVED),
        ]
        stats = reliability_stats(trials, k=1)["glm"]
        assert stats.pass_at_k == stats.pass_hat_k == 0.5
        assert stats.all_solved_tasks == 2

    def test_cost_per_reliable_solve_honesty(self) -> None:
        def _priced(task_id: str, outcome: TrialOutcome, cost: float | None) -> TrialResult:
            return _trial(task_id, "glm", outcome, rollout=1, cost_usd=cost)

        # unreported cost stays None (PRD §53-54)
        free = [_priced("t1", TrialOutcome.SOLVED, None)]
        assert reliability_stats(free, k=1)["glm"].cost_per_reliable_solve_usd is None
        # no reliable solve divides nothing
        failing = [_priced("t1", TrialOutcome.UNSOLVED, 0.5)]
        assert reliability_stats(failing, k=1)["glm"].cost_per_reliable_solve_usd is None
        # partially reported cost never turns into an average
        mixed = [_priced("t1", TrialOutcome.SOLVED, 0.5), _priced("t2", TrialOutcome.SOLVED, None)]
        assert reliability_stats(mixed, k=1)["glm"].cost_per_reliable_solve_usd is None
        # fully reported: ($0.30 + $0.90) over two all-solved tasks
        priced = [_priced("t1", TrialOutcome.SOLVED, 0.3), _priced("t2", TrialOutcome.SOLVED, 0.9)]
        assert reliability_stats(priced, k=1)["glm"].cost_per_reliable_solve_usd == 0.6

    def test_k_below_one_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            reliability_stats([], k=0)

    def test_model_round_trips_like_every_report_segment(self) -> None:
        stats = reliability_stats([_trial("t1", "glm", TrialOutcome.SOLVED)], k=1)["glm"]
        parsed = TargetReliability.model_validate_json(stats.model_dump_json())
        assert parsed == stats


# ------------------------------------------------------------ #13 persistence


class TestRolloutStorage:
    def test_pre_wave2_db_is_migrated_and_trials_still_load(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        legacy = TrialResult(
            trial_id="trial_old",
            run_id="run_old",
            benchmark_id="rb_b_old",
            task_id="task_old",
            target_id="glm",
            outcome=TrialOutcome.SOLVED,
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE trials("
            "  trial_id TEXT PRIMARY KEY, run_id TEXT, benchmark_id TEXT,"
            "  task_id TEXT, target_id TEXT, outcome TEXT, data_json TEXT,"
            "  created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO trials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "trial_old",
                "run_old",
                "rb_b_old",
                "task_old",
                "glm",
                "SOLVED",
                legacy.model_dump_json(),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
        conn.close()

        storage = Storage(db_path)
        columns = {row["name"] for row in storage.query("PRAGMA table_info(trials)")}
        assert "rollout" in columns
        trials = storage.list_trials()
        assert [t.trial_id for t in trials] == ["trial_old"]
        assert trials[0].rollout == 1  # historical rows read as rollout 1
        assert trials[0].outcome is TrialOutcome.SOLVED

        # new writes land in the migrated column too
        storage.save_trial(legacy.model_copy(update={"trial_id": "trial_new", "rollout": 2}))
        row = storage.query("SELECT rollout FROM trials WHERE trial_id = 'trial_new'")
        assert row[0]["rollout"] == 2


# --------------------------------------------------------------- #13 planning


class TestRolloutPlanning:
    def _plan(
        self,
        storage: Storage,
        fixture_repo: Path,
        *,
        rollouts: int,
        resume: bool,
        retry_failed: bool = False,
    ):
        from repobench.cli.services import plan_run
        from repobench.core.paths import ProjectPaths

        cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
        target = ExecutionTarget(
            id="fixer", harness="command", command=["true", "{workspace}"]
        )
        return plan_run(
            storage,
            ProjectPaths(fixture_repo),
            cfg,
            targets=[target],
            resume=resume,
            retry_failed=retry_failed,
            rollouts=rollouts,
        )

    def _seed_two_rollouts(self, storage: Storage) -> None:
        """Rollout 1 settled SOLVED, rollout 2 settled UNSOLVED in a previous run."""
        benchmark_id = storage.list_benchmarks()[0]["benchmark_id"]
        task_id = storage.benchmark_task_ids(benchmark_id)[0]
        storage.create_run("run_prev", benchmark_id)
        for rollout, outcome in ((1, TrialOutcome.SOLVED), (2, TrialOutcome.UNSOLVED)):
            storage.save_trial(
                TrialResult(
                    trial_id=f"trial_prev_{rollout}",
                    run_id="run_prev",
                    benchmark_id=benchmark_id,
                    task_id=task_id,
                    target_id="fixer",
                    rollout=rollout,
                    outcome=outcome,
                )
            )

    def test_fresh_run_expands_each_task_target_into_rollouts(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        plan = self._plan(storage, fixture_repo, rollouts=2, resume=False)
        assert plan.rollouts == 2
        assert [(t.task_id, target.id, rollout) for t, target, rollout in plan.pairs] == [
            (plan.tasks[0].task_id, "fixer", 1),
            (plan.tasks[0].task_id, "fixer", 2),
        ]

    def test_resume_without_flags_replans_nothing(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        self._seed_two_rollouts(storage)
        plan = self._plan(storage, fixture_repo, rollouts=2, resume=True)
        assert plan.pairs == []
        assert plan.already_complete == 2
        assert plan.retried == 0

    def test_retry_failed_replans_only_the_unsolved_rollout(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        self._seed_two_rollouts(storage)
        plan = self._plan(storage, fixture_repo, rollouts=2, resume=True, retry_failed=True)
        assert [rollout for _task, _target, rollout in plan.pairs] == [2]
        assert plan.retried == 1
        assert plan.already_complete == 1  # the SOLVED rollout 1 stays complete

    def test_zero_rollouts_rejected(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        with pytest.raises(UsageError):
            self._plan(storage, fixture_repo, rollouts=0, resume=False)


# --------------------------------------------------------------- #13 executor


class TestRolloutThreading:
    async def test_execute_carries_rollout_on_success(self, tmp_path: Path) -> None:
        from tests.test_runner import FIX_AGENT, _command_target, _executor, _make_task, _write_agent

        task = _make_task(tmp_path)
        fix_agent = _write_agent(tmp_path, "fix_rollout.py", FIX_AGENT)
        result = await _executor(tmp_path).execute(
            task, _command_target("fake-agent", fix_agent), rollout=3
        )
        assert result.outcome is TrialOutcome.SOLVED
        assert result.rollout == 3

    async def test_execute_carries_rollout_on_error_path(self, tmp_path: Path) -> None:
        from tests.test_runner import _command_target, _executor, _make_task

        task = _make_task(tmp_path)
        executor = _executor(
            tmp_path, adapter_lookup=lambda _harness: (_ for _ in ()).throw(UsageError("boom"))
        )
        result = await executor.execute(
            task, _command_target("x", tmp_path / "whatever.py"), rollout=2
        )
        assert result.outcome is TrialOutcome.SETUP_ERROR
        assert result.rollout == 2

    async def test_run_matrix_passes_each_triple_rollout_through(self, tmp_path: Path) -> None:
        from tests.test_runner import FIX_AGENT, _command_target, _executor, _make_task, _write_agent

        task = _make_task(tmp_path)
        fix_agent = _write_agent(tmp_path, "fix_matrix_rollout.py", FIX_AGENT)
        pairs = [
            (task, _command_target("fake-agent", fix_agent), rollout) for rollout in (1, 2)
        ]
        results = await run_matrix(pairs, _executor(tmp_path))
        assert sorted(result.rollout for result in results) == [1, 2]


# ----------------------------------------------------- #13 report/e2e wiring


class TestMultiRolloutEndToEnd:
    def test_run_rollouts_report_and_resume(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)

        result = _invoke("run", "fixer", "--rollouts", "2", "--yes", "--trust-custom-command")
        assert result.exit_code == 0, result.output
        # the cost multiplier is explicit before execution (PRD §103)
        assert "cost ×2" in result.output
        trials = storage.list_trials()
        assert sorted(trial.rollout for trial in trials) == [1, 2]
        assert all(trial.outcome is TrialOutcome.SOLVED for trial in trials)

        text = _invoke("report")
        assert text.exit_code == 0, text.output
        assert "Reliability — 2 rollouts per task" in text.output
        assert "pass@2" in text.output and "pass^2" in text.output
        assert text.output.index("Pareto frontier") < text.output.index("Reliability —")

        data = json.loads(_invoke("report", "--format", "json").output)
        reliability = data["reliability"]
        assert set(reliability) == {"fixer"}
        assert reliability["fixer"]["k"] == 2
        assert reliability["fixer"]["n_tasks"] == 1
        assert reliability["fixer"]["all_solved_tasks"] == 1
        assert reliability["fixer"]["pass_at_k"] == 1.0
        assert reliability["fixer"]["pass_hat_k"] == 1.0

        # resume after full completion re-plans nothing and re-runs nothing
        again = _invoke(
            "run", "fixer", "--rollouts", "2", "--yes", "--resume", "--trust-custom-command"
        )
        assert again.exit_code == 0, again.output
        assert "Already complete" in again.output
        assert len(storage.list_trials()) == 2

    def test_single_rollout_run_has_no_reliability_section(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        text = _invoke("report")
        assert "Reliability" not in text.output
        data = json.loads(_invoke("report", "--format", "json").output)
        assert data["reliability"] is None
