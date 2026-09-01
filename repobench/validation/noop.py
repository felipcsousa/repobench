"""No-op validation (PRD §78): BASE + hidden verifier must FAIL.

If the hidden verifier passes on the unmodified base, it cannot discriminate a
real solution and the task is rejected with NOOP_PASSES.
"""

from __future__ import annotations

from pathlib import Path

from repobench.config import ProjectConfig
from repobench.core.types import RejectionCode, TaskPackage
from repobench.validation._shared import (
    CheckResult,
    CheckSpec,
    _run_spec,
    get_test_command,
)

CHECK_NAME = "noop"

SPEC = CheckSpec(
    name=CHECK_NAME,
    command_getter=get_test_command,
    apply_verifier=True,
    invert=True,  # decisive exit 1 means the check PASSED
    fail_code=RejectionCode.NOOP_PASSES,
)


def check_noop(task: TaskPackage, project: ProjectConfig, *, workspaces_root: Path) -> CheckResult:
    return _run_spec(task, project, SPEC, workspaces_root=workspaces_root)
