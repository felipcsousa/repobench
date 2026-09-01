"""OpenCode adapter (PRD §23).

Uses `opencode run` for non-interactive execution. Output is text, so usage is
harvested defensively from any JSON blob near the end of stdout (OpenCode may
embed a JSON trailer with `usage`/`tokens` depending on provider). Fields are
mapped permissively (input_tokens/promptTokens/...) and never invented.
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

_TAIL_CHARS = 2000

_INPUT_KEYS = ("input_tokens", "promptTokens", "prompt_tokens", "input")
_CACHED_KEYS = ("cached_input_tokens", "cache_read_input_tokens", "cachedTokens", "cached")
_OUTPUT_KEYS = ("output_tokens", "completionTokens", "completion_tokens", "output")


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


def _usage_from_object(obj: dict) -> UsageRecord | None:
    for container_key in ("usage", "tokens"):
        container = obj.get(container_key)
        if isinstance(container, dict):
            usage = _usage_from_dict(container)
            if usage is not None:
                return usage
    return _usage_from_dict(obj)


class OpenCodeAdapter(HarnessAdapter):
    name = "opencode"
    binary = "opencode"
    capabilities = HarnessCapabilities(
        model_override=True,
        structured_output=True,
        token_usage=True,
        cost_usage=True,
        auto_approval=False,
        custom_provider=True,
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
        argv = ["opencode", "run"]
        if target.model:
            argv += ["--model", target.model]
        argv += list(target.args)
        argv.append(prompt)
        return CommandSpec(
            argv=argv,
            cwd=workspace,
            env={},
            timeout_seconds=timeout_seconds,
            output_mode=OutputMode.TEXT,
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        """Look for JSON blobs in the tail of stdout that carry usage/token data."""
        tail = (stdout or "")[-_TAIL_CHARS:]
        if "{" not in tail:
            return HarnessResult()
        usage: UsageRecord | None = None
        decoder = json.JSONDecoder()
        try:
            for idx, ch in enumerate(tail):
                if ch != "{":
                    continue
                try:
                    obj, _end = decoder.raw_decode(tail, idx)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                parsed = _usage_from_object(obj)
                if parsed is not None:
                    usage = parsed
        except Exception:
            return HarnessResult()
        if usage is None:
            return HarnessResult()
        return HarnessResult(usage=usage)
