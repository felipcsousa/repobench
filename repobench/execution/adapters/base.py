"""Harness adapter contract (PRD §19-20, §118-119).

Adapters translate an ExecutionTarget + Task into a local CommandSpec and parse harness
output. They never run the task themselves and never assume parity between harnesses.
"""

from __future__ import annotations

import abc
import shutil
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from repobench.core.types import CommandSpec, ExecutionTarget, UsageRecord


class HarnessCapabilities(BaseModel):
    """Declared capabilities, so the product never assumes false parity (PRD §119-120)."""

    model_override: bool = True
    structured_output: bool = False
    token_usage: bool = False
    cost_usage: bool = False
    auto_approval: bool = False
    custom_provider: bool = False


class HarnessDetection(BaseModel):
    installed: bool = False
    version: str | None = None
    path: str | None = None


class ValidationResult(BaseModel):
    valid: bool = True
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class HarnessResult(BaseModel):
    usage: UsageRecord | None = None
    notes: str | None = None


class HarnessAdapter(abc.ABC):
    name: ClassVar[str] = "abstract"
    binary: ClassVar[str] = ""
    capabilities: ClassVar[HarnessCapabilities] = HarnessCapabilities()

    def detect(self) -> HarnessDetection:
        path = shutil.which(self.binary)
        if not path:
            return HarnessDetection(installed=False)
        return HarnessDetection(installed=True, path=path, version=self.version())

    def version(self) -> str | None:
        """Best-effort binary version. Never performs inference (PRD §92)."""
        from repobench.execution.process import run_sync

        if not shutil.which(self.binary):
            return None
        try:
            r = run_sync([self.binary, "--version"], cwd=Path.cwd(), timeout_seconds=15)
        except Exception:
            return None
        if r.exit_code not in (0, None) or r.timed_out:
            return None
        lines = (r.stdout or r.stderr).strip().splitlines()
        return lines[0][:120] if lines else None

    @abc.abstractmethod
    def validate_target(self, target: ExecutionTarget) -> ValidationResult:
        """Structural validation only — no inference, no API calls (PRD §94)."""

    @abc.abstractmethod
    def build_command(
        self,
        target: ExecutionTarget,
        prompt: str,
        workspace: Path,
        *,
        task_id: str = "",
        target_id: str = "",
        timeout_seconds: int = 1200,
    ) -> CommandSpec:
        """Translate target + task into a local command spec."""

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        """Default: usage unknown. Never invent usage data (PRD §54)."""
        return HarnessResult()

    def cleanup(self) -> None:
        return None
