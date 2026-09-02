"""Incremental benchmark builds — reuse of previously validated tasks (issue #16, PRD §88).

`benchmark build --reuse-valid` skips the five historical-validation checks for
candidates whose deterministic task_id already holds status VALID in the tasks
table and whose on-disk package still loads. Task ids are content derived
(core.ids.new_task_id over PR number + base/merge SHAs), so "same task_id" ⇔
same PR content: reuse never changes what ships, only how much work a rebuild
does. The append-only task_validations log keeps the original evidence and gains
a "reused-previous-validation" row per reuse.
"""

from __future__ import annotations

from repobench.core.errors import RepoBenchError
from repobench.core.ids import new_task_id
from repobench.core.paths import ProjectPaths
from repobench.core.types import CandidateInfo, TaskPackage, TaskStatus
from repobench.storage.db import Storage
from repobench.validation._shared import CheckResult
from repobench.validation.pipeline import TaskValidationReport

REUSED_CHECK_NAME = "reused-previous-validation"
REUSED_CHECK_DETAILS = (
    "task validated by a previous build; validation skipped (--reuse-valid)"
)


def task_id_for(candidate: CandidateInfo) -> str:
    """Deterministic task id of a candidate — the exact derivation `_package_for`
    builds packages with, shared so reuse lookups can never diverge from it."""
    pr = candidate.pr
    return new_task_id(pr.number, pr.base_sha, pr.merge_sha)


def package_loads(paths: ProjectPaths, task_id: str) -> bool:
    """True when the content-addressed package directory exists and validates.

    A missing or corrupt package is not reusable: the build rebuilds it fresh
    and revalidates, keeping a VALID tasks row honest about real artifacts."""
    try:
        TaskPackage.load(paths.task_dir(task_id))
    except (FileNotFoundError, RepoBenchError, OSError):
        return False
    return True


def reusable_task_ids(storage: Storage, paths: ProjectPaths) -> set[str]:
    """Task ids eligible for validation reuse (issue #16): status VALID in the
    tasks table AND a package that still loads from disk. Pool membership is
    enforced separately by the build loop, which iterates candidates only."""
    return {
        task_id
        for task_id in storage.task_ids_with_status(TaskStatus.VALID.value)
        if package_loads(paths, task_id)
    }


def reused_validation_report(task_id: str) -> TaskValidationReport:
    """Stand-in report for a reused task: VALID with the single reuse check, so
    TaskBuildResult stays homogeneous — health's passed_ratio counts one passed
    check (honest: the checks did pass previously and the append-only log keeps
    the original evidence rows)."""
    return TaskValidationReport(
        task_id=task_id,
        status=TaskStatus.VALID,
        rejection_code=None,
        duration_ms=0,
        checks=[
            CheckResult(
                name=REUSED_CHECK_NAME,
                passed=True,
                code=None,
                details=REUSED_CHECK_DETAILS,
            )
        ],
    )
