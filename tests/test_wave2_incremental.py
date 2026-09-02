"""Wave 2 incremental benchmark builds (issue #16, PRD §88): `--reuse-valid`
skips re-validating tasks that already validated VALID in a previous build while
keeping persistence, leakage gating and the shipped sample identical — plus unit
coverage of the reuse eligibility rules and the --force-revalidate override."""

from __future__ import annotations

from pathlib import Path

import pytest

from repobench.benchmark.reuse import (
    REUSED_CHECK_DETAILS,
    REUSED_CHECK_NAME,
    reusable_task_ids,
    reused_validation_report,
)
from repobench.core.paths import ProjectPaths
from repobench.core.types import TaskMetadata, TaskStatus
from repobench.storage.db import Storage
from tests.test_e2e import _fast_forward, _invoke

PACKAGE_FILES_BUT_METADATA = ("base.tar", "instruction.md", "gold.patch", "verifier.patch")


def _validation_rows(storage: Storage, task_id: str, kind: str | None = None) -> list[dict]:
    sql = "SELECT kind, result FROM task_validations WHERE task_id = ?"
    params: tuple | list = (task_id,)
    if kind is not None:
        sql += " AND kind = ?"
        params = (task_id, kind)
    return storage.query(sql, params)


def _shipped_task_ids(storage: Storage) -> list[str]:
    newest = storage.list_benchmarks()[0]  # ordered by created_at DESC
    return storage.benchmark_task_ids(newest["benchmark_id"])


def _write_loadable_package(directory: Path, task_id: str) -> None:
    """Minimal on-disk package TaskPackage.load accepts: file presence plus
    parseable metadata is all load checks — base.tar content is irrelevant."""
    directory.mkdir(parents=True, exist_ok=True)
    metadata = TaskMetadata(task_id=task_id, base_sha="base", gold_sha="gold")
    for name in PACKAGE_FILES_BUT_METADATA:
        (directory / name).write_bytes(b"")
    (directory / "metadata.json").write_text(metadata.model_dump_json())


# --------------------------------------------------------- #16 eligibility rules


class TestReuseEligibility:
    def test_valid_status_with_loadable_package_is_reusable(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "state.db")
        paths = ProjectPaths(tmp_path)
        storage.save_task("t_ok", data={}, status="VALID")
        _write_loadable_package(paths.task_dir("t_ok"), "t_ok")
        assert reusable_task_ids(storage, paths) == {"t_ok"}

    def test_rejected_status_is_not_reusable_despite_loadable_package(
        self, tmp_path: Path
    ) -> None:
        storage = Storage(tmp_path / "state.db")
        paths = ProjectPaths(tmp_path)
        storage.save_task("t_rej", data={}, status="REJECTED")
        _write_loadable_package(paths.task_dir("t_rej"), "t_rej")
        assert reusable_task_ids(storage, paths) == set()

    def test_missing_package_dir_is_not_reusable(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "state.db")
        paths = ProjectPaths(tmp_path)
        storage.save_task("t_nopkg", data={}, status="VALID")
        assert not paths.task_dir("t_nopkg").exists()
        assert reusable_task_ids(storage, paths) == set()

    def test_corrupt_metadata_is_not_reusable(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "state.db")
        paths = ProjectPaths(tmp_path)
        storage.save_task("t_bad", data={}, status="VALID")
        directory = paths.task_dir("t_bad")
        directory.mkdir(parents=True)
        for name in PACKAGE_FILES_BUT_METADATA:
            (directory / name).write_bytes(b"")
        (directory / "metadata.json").write_text("{not json")
        assert reusable_task_ids(storage, paths) == set()

    def test_reused_report_is_valid_with_exactly_one_passing_check(self) -> None:
        report = reused_validation_report("t_x")
        assert report.status is TaskStatus.VALID
        assert report.rejection_code is None
        assert report.duration_ms == 0
        assert len(report.checks) == 1
        check = report.checks[0]
        assert check.name == REUSED_CHECK_NAME
        assert check.passed is True
        assert check.code is None
        assert check.details == REUSED_CHECK_DETAILS


# ------------------------------------------------------------- #16 e2e behavior


class TestIncrementalBuild:
    def test_reuse_valid_skips_expensive_checks_and_records_reuse(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_ids = _shipped_task_ids(storage)
        assert len(task_ids) == 1  # the fixture yields exactly one valid task
        task_id = task_ids[0]
        determinism_before = len(_validation_rows(storage, task_id, "determinism"))
        assert determinism_before >= 1  # sanity: the first build really validated

        built = _invoke("benchmark", "build", "--reuse-valid")
        assert built.exit_code == 0, built.output
        assert "Reused valid tasks" in built.output
        assert "(reused)" in built.output

        # the append-only log records the reuse event...
        reused_rows = _validation_rows(storage, task_id, REUSED_CHECK_NAME)
        assert len(reused_rows) == 1
        assert reused_rows[0]["result"] == "passed"
        # ...but none of the expensive checks ran again
        assert len(_validation_rows(storage, task_id, "determinism")) == determinism_before
        # the tasks row stays VALID and the shipped sample is unchanged
        status = storage.query(
            "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
        )[0]["status"]
        assert status == "VALID"
        assert _shipped_task_ids(storage) == [task_id]

    def test_default_build_without_flag_still_revalidates(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_id = _shipped_task_ids(storage)[0]
        determinism_before = len(_validation_rows(storage, task_id, "determinism"))

        built = _invoke("benchmark", "build")
        assert built.exit_code == 0, built.output
        assert len(_validation_rows(storage, task_id, "determinism")) > determinism_before
        assert _validation_rows(storage, task_id, REUSED_CHECK_NAME) == []

    def test_force_revalidate_wins_over_reuse_valid(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        task_id = _shipped_task_ids(storage)[0]
        determinism_before = len(_validation_rows(storage, task_id, "determinism"))

        built = _invoke("benchmark", "build", "--reuse-valid", "--force-revalidate")
        assert built.exit_code == 0, built.output
        assert len(_validation_rows(storage, task_id, "determinism")) > determinism_before
        assert _validation_rows(storage, task_id, REUSED_CHECK_NAME) == []

    def test_reuse_never_changes_what_ships(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        ids_first = _shipped_task_ids(storage)

        assert _invoke("benchmark", "build", "--reuse-valid").exit_code == 0
        # deterministic task ids: reuse only changes how much work happens
        assert _shipped_task_ids(storage) == ids_first
