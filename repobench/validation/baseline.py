"""Preflight baseline check (PRD §77): BASE + dependencies must pass the configured
regression command. If the base is already broken, the task is BASELINE_BROKEN."""

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

CHECK_NAME = "baseline"

SPEC = CheckSpec(
    name=CHECK_NAME,
    command_getter=get_regression_command,
    fail_code=RejectionCode.BASELINE_BROKEN,
    pass_description="baseline passes on BASE",
    fail_description="baseline fails on BASE",
)


def check_baseline(task: TaskPackage, project: ProjectConfig, *, workspaces_root: Path) -> CheckResult:
    return _run_spec(task, project, SPEC, workspaces_root=workspaces_root)
