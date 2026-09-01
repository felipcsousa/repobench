"""Codex CLI adapter (PRD §22).

Uses `codex exec --json` for non-interactive execution; stdout is JSONL where
token accounting appears in objects carrying `token_count` / `usage` / `info`.
Parsing is deliberately defensive: garbage lines are ignored and usage stays
unknown rather than being invented (PRD §54).
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

# Common spellings for token counts across codex event shapes.
_INPUT_KEYS = ("input", "input_tokens", "prompt_tokens", "promptTokens")
_CACHED_KEYS = ("cached_input", "cached_input_tokens", "cached_tokens", "cache_read_input_tokens")
_OUTPUT_KEYS = ("output", "output_tokens", "completion_tokens", "completionTokens")
_NESTED_KEYS = ("total_token_usage", "token_usage", "last_token_usage", "usage", "token_count")


def _grab(data: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return None


def _usage_from_dict(data: dict) -> UsageRecord | None:
    input_tokens = _grab(data, _INPUT_KEYS)
    cached = _grab(data, _CACHED_KEYS)
    output = _grab(data, _OUTPUT_KEYS)
    if input_tokens is None and cached is None and output is None:
        return None
    return UsageRecord(
        input_tokens=input_tokens, cached_input_tokens=cached, output_tokens=output
    )


def _usage_from_payload(payload: dict) -> UsageRecord | None:
    direct = _usage_from_dict(payload)
    if direct is not None:
        return direct
    for key in _NESTED_KEYS:
        nested = payload.get(key)
        if isinstance(nested, dict):
            usage = _usage_from_dict(nested)
            if usage is not None:
                return usage
    return None


class CodexAdapter(HarnessAdapter):
    name = "codex"
    binary = "codex"
    capabilities = HarnessCapabilities(
        model_override=True,
        structured_output=True,
        token_usage=True,
        cost_usage=False,
    )

    def validate_target(self, target: ExecutionTarget) -> ValidationResult:
        # The registry only pairs an adapter with its own harness name, so there is
        # nothing structural left to reject for official Tier-1 targets (PRD §94).
        return ValidationResult()

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
        argv = ["codex", "exec", "--json"]
        if target.model:
            argv += ["--model", target.model]
        argv += list(target.args)
        argv.append(prompt)
        return CommandSpec(
            argv=argv,
            cwd=workspace,
            env={},
            timeout_seconds=timeout_seconds,
            output_mode=OutputMode.JSONL,
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        """Scan JSONL lines; the last event carrying token counts wins (final totals)."""
        usage: UsageRecord | None = None
        try:
            for line in (stdout or "").splitlines():
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    obj = json.loads(stripped)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                for key in ("token_count", "usage", "info"):
                    payload = obj.get(key)
                    if isinstance(payload, dict):
                        parsed = _usage_from_payload(payload)
                        if parsed is not None:
                            usage = parsed
                        break
        except Exception:
            return HarnessResult()
        if usage is None:
            return HarnessResult()
        return HarnessResult(usage=usage)
