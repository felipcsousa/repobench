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


def _float_field(data: dict, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    binary = "claude"
    capabilities = HarnessCapabilities(
        model_override=True,
        structured_output=True,
        token_usage=True,
        # total_cost_usd is parsed from the result JSON (issue #17).
        cost_usage=True,
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
        """The result object is the last stdout line that starts with '{'.

        Token counts live under `usage`; the top level also carries
        `total_cost_usd`, `num_turns` and `tool_use_count` — each lifted only
        when actually present, never invented (issue #17, PRD §54).
        """
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
            usage_data = data.get("usage")
            usage_dict = usage_data if isinstance(usage_data, dict) else {}
            input_tokens = _int_field(usage_dict, "input_tokens")
            cached_input_tokens = _int_field(usage_dict, "cache_read_input_tokens")
            output_tokens = _int_field(usage_dict, "output_tokens")
            reported_cost_usd = _float_field(data, "total_cost_usd")
            requests = _int_field(data, "num_turns")
            tool_calls = _int_field(data, "tool_use_count")
            if (
                input_tokens is None
                and output_tokens is None
                and cached_input_tokens is None
                and reported_cost_usd is None
                and requests is None
                and tool_calls is None
            ):
                return HarnessResult()
            return HarnessResult(
                usage=UsageRecord(
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                    requests=requests,
                    tool_calls=tool_calls,
                    reported_cost_usd=reported_cost_usd,
                )
            )
        except Exception:
            # Garbage output must never crash a trial; usage stays unknown (PRD §54).
            return HarnessResult()
