"""Task validation pipeline (PRD §77-82).

Runs the checks in order — baseline, noop, oracle, regression, determinism — and
stops at the first failure. Rejection codes BASELINE_BROKEN, NOOP_PASSES,
GOLD_FAILS, GOLD_REGRESSION and FLAKY_VERIFIER are hard task rejections; an
inconclusive check (code=None, i.e. a verifier infrastructure problem) is treated
as ENVIRONMENT_UNSUPPORTED so that a REJECTED report always carries a code. The
invariant is simply: code None ⇒ inconclusive. A missing project.test_command
rejects the task up front — without one no decisive check can execute, so VALID
would be vacuous. A leakage report at risk HIGH rejects the task with
LEAKAGE_HIGH before any command is run; MEDIUM is a candidate warning and LOW
is clean — only HIGH gates (PRD §44-46, §87). The leakage score is
informational and never decides the verdict.
"""

from __future__ import annotations

import time
from pathlib import Path

import pydantic

from repobench.config import ProjectConfig
from repobench.core.types import RejectionCode, TaskPackage, TaskStatus
from repobench.tasks.leakage import LeakageReport
from repobench.validation._shared import CheckResult, split_command
from repobench.validation.baseline import check_baseline
from repobench.validation.determinism import check_determinism
from repobench.validation.noop import check_noop
from repobench.validation.oracle import check_oracle
from repobench.validation.regression import check_regression

# Retained for backward compatibility with the pre-§45 score gate (external
# tooling and old persisted references may still import it). No longer used
# for the verdict: the gate is `risk != "HIGH"` (PRD §45).
LEAKAGE_SCORE_THRESHOLD = 40

_CHECKS = (check_baseline, check_noop, check_oracle, check_regression, check_determinism)


class TaskValidationReport(pydantic.BaseModel):
    task_id: str
    status: TaskStatus  # VALID or REJECTED
    rejection_code: RejectionCode | None
    checks: list[CheckResult]
    duration_ms: int = 0


class TaskValidator:
    """Validates reconstructed task packages against the local project environment."""

    def __init__(self, project: ProjectConfig, workspaces_root: Path):
        self.project = project
        self.workspaces_root = Path(workspaces_root)

    def validate(
        self,
        task: TaskPackage,
        *,
        leakages: LeakageReport | None = None,
    ) -> TaskValidationReport:
        start = time.monotonic()
        checks: list[CheckResult] = []

        def finish(status: TaskStatus, code: RejectionCode | None) -> TaskValidationReport:
            return TaskValidationReport(
                task_id=task.task_id,
                status=status,
                rejection_code=code,
                checks=checks,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        if leakages is not None:
            # Only HIGH is a hard reject (PRD §45); MEDIUM is a candidate
            # warning and LOW is clean. The score is informational (PRD §46).
            leakage_ok = leakages.risk != "HIGH"
            details = f"leakage risk {leakages.risk} (score {leakages.score}/100, informational)"
            if leakages.evidence:
                details += "; evidence: " + "; ".join(leakages.evidence)
            checks.append(
                CheckResult(
                    name="leakage",
                    passed=leakage_ok,
                    code=None if leakage_ok else RejectionCode.LEAKAGE_HIGH,
                    details=details,
                )
            )
            if not leakage_ok:
                return finish(TaskStatus.REJECTED, RejectionCode.LEAKAGE_HIGH)

        # Vacuous-VALID guard: a task can never be VALID without an executed
        # decisive check, and every decisive check needs project.test_command.
        if split_command(self.project.test_command) is None:
            checks.append(
                CheckResult(
                    name="environment",
                    passed=False,
                    code=RejectionCode.ENVIRONMENT_UNSUPPORTED,
                    details="no project.test_command configured — cannot validate tasks",
                )
            )
            return finish(TaskStatus.REJECTED, RejectionCode.ENVIRONMENT_UNSUPPORTED)

        for check_fn in _CHECKS:
            result = check_fn(task, self.project, workspaces_root=self.workspaces_root)
            checks.append(result)
            if result.passed is False:
                # code None ⇒ inconclusive (verifier/environment problem), never a task defect.
                code = (
                    result.code
                    if result.code is not None
                    else RejectionCode.ENVIRONMENT_UNSUPPORTED
                )
                return finish(TaskStatus.REJECTED, code)

        return finish(TaskStatus.VALID, None)
