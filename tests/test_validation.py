"""Tests for validation checks and the TaskValidator pipeline (PRD §77-82).

Uses the shared buggy-calculator fixture repo: the "PR" fixes it and adds a
hidden test. Verifier commands run the real venv pytest against freshly
materialized workspaces.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.fixtures.gitutil import TEST_MULTIPLY, build_repo, make_candidate
from repobench.config import ProjectConfig
from repobench.core.types import (
    RejectionCode,
    TaskPackage,
    TaskStatus,
)
from repobench.tasks.leakage import LeakageReport
from repobench.tasks.reconstruction import build_task_package
from repobench.validation.baseline import check_baseline
from repobench.validation.determinism import check_determinism
from repobench.validation.noop import check_noop
from repobench.validation.oracle import check_oracle
from repobench.validation.pipeline import TaskValidator, TaskValidationReport
from repobench.validation.regression import check_regression

PYTEST_CMD = f"{sys.executable} -m pytest -q"


def make_task(tmp_path: Path, **repo_kwargs) -> TaskPackage:
    fx = build_repo(tmp_path, **repo_kwargs)
    return build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")


def make_project(**overrides) -> ProjectConfig:
    kwargs: dict = {"test_command": PYTEST_CMD, "regression_command": None}
    kwargs.update(overrides)
    return ProjectConfig(**kwargs)


def by_name(report) -> dict:
    return {check.name: check for check in report.checks}


# ------------------------------------------------------------------- pipeline


def test_pipeline_happy_path(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    report = TaskValidator(make_project(), workspaces_root=tmp_path / "ws").validate(task)

    assert isinstance(report, TaskValidationReport)
    assert report.task_id == task.task_id
    assert report.status == TaskStatus.VALID
    assert report.rejection_code is None
    assert report.duration_ms >= 0
    checks = by_name(report)
    assert checks["baseline"].passed is None  # skipped: no regression_command
    assert checks["noop"].passed is True  # hidden test fails on base
    assert checks["oracle"].passed is True  # gold + hidden test passes
    assert checks["regression"].passed is None  # skipped: no regression_command
    assert checks["determinism"].passed is True
    assert all(check.duration_ms >= 0 for check in report.checks)


def test_pipeline_broken_gold_rejected(tmp_path: Path) -> None:
    # PR touches calculator.py without fixing the bug: oracle must fail.
    task = make_task(tmp_path, number=10, fix_bug=False)
    report = TaskValidator(make_project(), workspaces_root=tmp_path / "ws").validate(task)

    assert report.status == TaskStatus.REJECTED
    assert report.rejection_code == RejectionCode.GOLD_FAILS
    checks = by_name(report)
    assert checks["noop"].passed is True
    assert checks["oracle"].passed is False
    assert "regression" not in checks and "determinism" not in checks  # stopped early


def test_pipeline_noop_passes_rejected(tmp_path: Path) -> None:
    # Hidden "verifier" asserts existing behavior: it passes on the unmodified base.
    task = make_task(
        tmp_path,
        number=11,
        test_name="test_multiply.py",
        test_source=TEST_MULTIPLY,
    )
    project = make_project()
    result = check_noop(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code == RejectionCode.NOOP_PASSES

    report = TaskValidator(project, workspaces_root=tmp_path / "ws").validate(task)
    assert report.status == TaskStatus.REJECTED
    assert report.rejection_code == RejectionCode.NOOP_PASSES
    assert by_name(report)["baseline"].passed is None


def test_pipeline_without_test_command_cannot_be_valid(tmp_path: Path) -> None:
    # Vacuous-VALID guard: with no test_command every decisive check would skip,
    # so VALID must be unreachable and the pipeline must reject up front.
    task = make_task(tmp_path)
    for project in (
        ProjectConfig(),  # nothing configured at all
        ProjectConfig(test_command=None, regression_command=PYTEST_CMD),
    ):
        report = TaskValidator(project, workspaces_root=tmp_path / "ws").validate(task)
        assert report.status == TaskStatus.REJECTED
        assert report.rejection_code == RejectionCode.ENVIRONMENT_UNSUPPORTED
        assert [check.name for check in report.checks] == ["environment"]
        guard = report.checks[0]
        assert guard.passed is False
        assert "no project.test_command configured" in guard.details
    assert not (tmp_path / "ws").exists()  # no workspace was ever materialized


def test_pipeline_install_failure_is_inconclusive(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(install_command=f'{sys.executable} -c "import sys; sys.exit(3)"')
    report = TaskValidator(project, workspaces_root=tmp_path / "ws").validate(task)

    assert report.status == TaskStatus.REJECTED
    assert report.rejection_code == RejectionCode.ENVIRONMENT_UNSUPPORTED
    noop = by_name(report)["noop"]
    assert noop.passed is False
    assert noop.code is None  # environment problem, not a task defect
    assert "install command failed" in noop.details


def test_pipeline_verifier_exit_2_is_inconclusive(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(test_command=f'{sys.executable} -c "import sys; sys.exit(2)"')
    report = TaskValidator(project, workspaces_root=tmp_path / "ws").validate(task)

    assert report.status == TaskStatus.REJECTED
    assert report.rejection_code == RejectionCode.ENVIRONMENT_UNSUPPORTED
    noop = by_name(report)["noop"]
    assert noop.passed is False
    assert noop.code is None
    assert "exited 2" in noop.details


def test_pipeline_leakage_gate_rejects_before_running_commands(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    report = TaskValidator(make_project(), workspaces_root=tmp_path / "ws").validate(
        task,
        leakages=LeakageReport(checks={}, score=30, findings=["leak found"]),
    )
    assert report.status == TaskStatus.REJECTED
    assert report.rejection_code == RejectionCode.LEAKAGE_HIGH
    assert [check.name for check in report.checks] == ["leakage"]
    assert report.checks[0].passed is False
    assert "leak found" in report.checks[0].details


def test_pipeline_leakage_threshold_boundary(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    validator = TaskValidator(make_project(), workspaces_root=tmp_path / "ws")

    rejected = validator.validate(task, leakages=LeakageReport(checks={}, score=39, findings=[]))
    assert rejected.status == TaskStatus.REJECTED
    assert rejected.rejection_code == RejectionCode.LEAKAGE_HIGH
    assert [check.name for check in rejected.checks] == ["leakage"]

    passed = validator.validate(task, leakages=LeakageReport(checks={}, score=40, findings=[]))
    assert passed.status == TaskStatus.VALID
    assert by_name(passed)["leakage"].passed is True


def test_pipeline_leakage_gate_passes_when_score_is_high(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    report = TaskValidator(make_project(), workspaces_root=tmp_path / "ws").validate(
        task,
        leakages=LeakageReport(checks={}, score=78, findings=[]),
    )
    assert report.status == TaskStatus.VALID
    assert by_name(report)["leakage"].passed is True


# ------------------------------------------------------------------- baseline


def test_check_baseline_passes_on_healthy_base(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(regression_command=PYTEST_CMD)
    result = check_baseline(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is True
    assert result.code is None


def test_check_baseline_broken(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(
        regression_command=f'{sys.executable} -c "import sys; sys.exit(1)"'
    )
    result = check_baseline(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code == RejectionCode.BASELINE_BROKEN
    assert result.details


def test_check_baseline_skipped_without_regression_command(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = check_baseline(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is None
    assert "regression_command" in result.details


# --------------------------------------------------------------------- oracle


def test_check_oracle_passes_with_real_gold(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = check_oracle(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is True
    assert result.code is None


def test_check_oracle_gold_fails(tmp_path: Path) -> None:
    task = make_task(tmp_path, number=10, fix_bug=False)
    result = check_oracle(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code == RejectionCode.GOLD_FAILS
    assert result.details


def test_check_install_failure_is_inconclusive(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(install_command=f'{sys.executable} -c "import sys; sys.exit(3)"')
    result = check_oracle(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code is None
    assert "install command failed" in result.details


def test_check_gold_patch_apply_failure_rejects(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    task.gold_patch.write_text("this is not a valid git patch\n")
    result = check_oracle(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code == RejectionCode.GOLD_FAILS  # task defect, not verifier trouble
    assert result.details.startswith("gold patch failed to apply")


def test_check_verifier_patch_apply_failure_is_inconclusive(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    task.verifier_patch.write_text("this is not a valid git patch\n")
    result = check_noop(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code is None
    assert result.details.startswith("verifier patch failed to apply")


def test_check_spawn_failure_is_inconclusive(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(test_command="/repobench/no/such/binary --run")
    result = check_noop(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code is None
    assert "spawn failed" in result.details


# ----------------------------------------------------------------- regression


def test_check_regression_passes_with_gold(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(regression_command=PYTEST_CMD)
    result = check_regression(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is True
    assert result.code is None


def test_check_regression_gold_regression(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    project = make_project(
        regression_command=f'{sys.executable} -c "import sys; sys.exit(1)"'
    )
    result = check_regression(task, project, workspaces_root=tmp_path / "ws")
    assert result.passed is False
    assert result.code == RejectionCode.GOLD_REGRESSION


def test_check_regression_skipped_without_command(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = check_regression(task, make_project(), workspaces_root=tmp_path / "ws")
    assert result.passed is None


# ---------------------------------------------------------------- determinism


def test_check_determinism_stable(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = check_determinism(task, make_project(), workspaces_root=tmp_path / "ws", runs=2)
    assert result.passed is True
    assert result.code is None
    assert "2/2" in result.details


def test_check_determinism_flaky(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    counter = tmp_path / "invocations.txt"
    script = tmp_path / "flaky_runner.py"
    # Fails on the first invocation only, then delegates to the real pytest suite.
    script.write_text(
        "import pathlib, subprocess, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "n = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(n + 1))\n"
        "if n == 0:\n"
        "    sys.exit(1)\n"
        f"sys.exit(subprocess.run([{str(sys.executable)!r}, '-m', 'pytest', '-q']).returncode)\n"
    )
    project = make_project(test_command=f"{sys.executable} {script}")
    result = check_determinism(task, project, workspaces_root=tmp_path / "ws", runs=2)
    assert result.passed is False
    assert result.code == RejectionCode.FLAKY_VERIFIER
    assert "not deterministic" in result.details


def test_check_determinism_gold_apply_failure_is_not_flaky(tmp_path: Path) -> None:
    # A gold patch that never applies fails deterministically: GOLD_FAILS passes
    # through instead of FLAKY_VERIFIER.
    task = make_task(tmp_path)
    task.gold_patch.write_text("this is not a valid git patch\n")
    result = check_determinism(task, make_project(), workspaces_root=tmp_path / "ws", runs=2)
    assert result.passed is False
    assert result.code == RejectionCode.GOLD_FAILS
    assert result.code is not RejectionCode.FLAKY_VERIFIER
    assert result.details.startswith("gold patch failed to apply")


def test_check_determinism_skipped_without_test_command(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    result = check_determinism(
        task, ProjectConfig(test_command=None), workspaces_root=tmp_path / "ws", runs=2
    )
    assert result.passed is None
