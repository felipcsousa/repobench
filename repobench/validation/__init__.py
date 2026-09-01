"""Validation checks and pipeline (PRD §77-82)."""
from __future__ import annotations

from repobench.validation._shared import CheckResult, CheckSpec
from repobench.validation.baseline import check_baseline
from repobench.validation.determinism import check_determinism
from repobench.validation.noop import check_noop
from repobench.validation.oracle import check_oracle
from repobench.validation.pipeline import TaskValidator, TaskValidationReport
from repobench.validation.regression import check_regression

__all__ = [
    "CheckResult",
    "CheckSpec",
    "TaskValidationReport",
    "TaskValidator",
    "check_baseline",
    "check_determinism",
    "check_noop",
    "check_oracle",
    "check_regression",
]
