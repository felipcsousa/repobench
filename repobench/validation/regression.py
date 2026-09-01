"""Regression validation (PRD §80): the GOLD solution must still pass the configured
regression command. If it breaks existing behavior, the task is GOLD_REGRESSION.

The hidden verifier is not applied here — existing behavior is what's under test.
"""

from __future__ import annotations

from pathlib import Path

from repobench.config import ProjectConfig
from repobench.core.types import RejectionCode, TaskPackage
from repobench.validation._shared import (
    CheckResult,
    CheckSpec,
    _run_spec,
    get_regression_command,
)

CHECK_NAME = "regression"

SPEC = CheckSpec(
    name=CHECK_NAME,
    command_getter=get_regression_command,
    apply_gold=True,
    fail_code=RejectionCode.GOLD_REGRESSION,
)


def check_regression(task: TaskPackage, project: ProjectConfig, *, workspaces_root: Path) -> CheckResult:
    return _run_spec(task, project, SPEC, workspaces_root=workspaces_root)
