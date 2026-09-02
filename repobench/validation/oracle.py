"""Oracle validation (PRD §79): BASE + GOLD implementation + hidden verifier must PASS.

GOLD exists only to prove the verifier can be satisfied — it is not an expected
patch (PRD §64). If the oracle fails, the task is rejected with GOLD_FAILS.
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

CHECK_NAME = "oracle"

SPEC = CheckSpec(
    name=CHECK_NAME,
    command_getter=get_test_command,
    apply_gold=True,
    apply_verifier=True,
    fail_code=RejectionCode.GOLD_FAILS,
    pass_description="gold + hidden verifier passes",
    fail_description="gold solution fails the hidden verifier",
)


def check_oracle(task: TaskPackage, project: ProjectConfig, *, workspaces_root: Path) -> CheckResult:
    return _run_spec(task, project, SPEC, workspaces_root=workspaces_root)
