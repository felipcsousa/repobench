"""Claude Code adapter (PRD §21).

Uses the non-interactive `-p/--print` mode with `--output-format json` so the
final result (including token usage) can be parsed from stdout. No shell
interpolation: the command is a plain argv list (PRD §20).
"""

from __future__ import annotations

import json
from pathlib import Path

from repobench.core.types import CommandSpec, ExecutionTarget, OutputMode, UsageRecord
from repobench.execution.adapters.base import (
    HarnessAdapter,
    HarnessCapabilities,
    HarnessResult,
    ValidationResult,
)


def _int_field(data: dict, key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    binary = "claude"
    capabilities = HarnessCapabilities(
        model_override=True,
        structured_output=True,
        token_usage=True,
        cost_usage=False,
        auto_approval=True,
        custom_provider=False,
    )

    def validate_target(self, target: ExecutionTarget) -> ValidationResult:
        warnings: list[str] = []
        if target.model is None:
            warnings.append("no model configured; using harness default model")
        return ValidationResult(valid=True, errors=[], warnings=warnings)

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
        argv = ["claude", "-p", prompt]
        if target.model:
            argv += ["--model", target.model]
        argv += ["--output-format", "json"]
        argv += list(target.args)
        return CommandSpec(
            argv=argv,
            cwd=workspace,
            env={},
            timeout_seconds=timeout_seconds,
            output_mode=OutputMode.JSON,
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        """The result object is the last stdout line that starts with '{'."""
        try:
            last_json_line: str | None = None
            for line in (stdout or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("{"):
                    last_json_line = stripped
            if last_json_line is None:
                return HarnessResult()
            data = json.loads(last_json_line)
            if not isinstance(data, dict):
                return HarnessResult()
            usage = data.get("usage")
            if not isinstance(usage, dict):
                return HarnessResult()
            input_tokens = _int_field(usage, "input_tokens")
            cached_input_tokens = _int_field(usage, "cache_read_input_tokens")
            output_tokens = _int_field(usage, "output_tokens")
            if input_tokens is None and output_tokens is None and cached_input_tokens is None:
                return HarnessResult()
            return HarnessResult(
                usage=UsageRecord(
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                )
            )
        except Exception:
            # Garbage output must never crash a trial; usage stays unknown (PRD §54).
            return HarnessResult()
