"""Determinism check (PRD §81): the oracle verification is executed N times (default 3)
on fresh workspaces and must pass every time, otherwise the verifier is FLAKY_VERIFIER."""

from __future__ import annotations

import time
from pathlib import Path

from repobench.config import ProjectConfig
from repobench.core.types import RejectionCode, TaskPackage
from repobench.validation._shared import GOLD_APPLY_FAILED, CheckResult, _run_spec, elapsed_ms
from repobench.validation.oracle import SPEC as ORACLE_SPEC

CHECK_NAME = "determinism"


def check_determinism(
    task: TaskPackage,
    project: ProjectConfig,
    *,
    workspaces_root: Path,
    runs: int = 3,
) -> CheckResult:
    start = time.monotonic()
    runs = max(1, runs)
    results = [
        _run_spec(task, project, ORACLE_SPEC, workspaces_root=workspaces_root)
        for _ in range(runs)
    ]
    duration = sum(r.duration_ms for r in results) or elapsed_ms(start)

    def result_of(
        passed: bool | None, code: RejectionCode | None = None, details: str = ""
    ) -> CheckResult:
        return CheckResult(
            name=CHECK_NAME, passed=passed, code=code, details=details, duration_ms=duration
        )

    if all(r.passed for r in results):
        return result_of(True, details=f"oracle verification passed {runs}/{runs} runs")
    if all(r.passed is None for r in results):
        return result_of(None, details=results[0].details)

    failures = [r for r in results if r.passed is False]
    # A gold patch that cannot apply fails deterministically on every run: that is
    # a task defect (GOLD_FAILS), not verifier flakiness.
    apply_failures = [r for r in failures if r.details.startswith(GOLD_APPLY_FAILED)]
    if apply_failures:
        return result_of(False, RejectionCode.GOLD_FAILS, apply_failures[0].details)
    hard_failures = [r for r in failures if r.code is not None]
    if hard_failures:
        return result_of(
            False,
            RejectionCode.FLAKY_VERIFIER,
            f"oracle verification is not deterministic: {len(hard_failures)}/{runs} runs "
            f"failed: {hard_failures[0].details}",
        )
    return result_of(
        False,
        details=f"inconclusive oracle runs ({len(failures)}/{runs}): {failures[0].details}",
    )
