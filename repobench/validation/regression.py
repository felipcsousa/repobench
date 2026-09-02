"""Regression validation (PRD §80): the GOLD solution must still pass the configured
regression command. If it breaks existing behavior, the task is GOLD_REGRESSION.

The hidden verifier is applied together with gold: a PR may legitimately update
existing tests (changed expected values live in the verifier patch), so "existing
behavior" is what the PR's final test suite says it is. Running gold against the
stale BASE tests would fail those updated expectations and reject valid tasks
with false GOLD_REGRESSIONs.
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
    apply_verifier=True,  # judge existing behavior against the PR's own test changes
    fail_code=RejectionCode.GOLD_REGRESSION,
    pass_description="gold + the PR's test changes pass the regression command",
    fail_description="gold + the PR's test changes fail the regression command",
)


def check_regression(task: TaskPackage, project: ProjectConfig, *, workspaces_root: Path) -> CheckResult:
    return _run_spec(task, project, SPEC, workspaces_root=workspaces_root)
