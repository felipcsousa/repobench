"""OpenCode adapter (PRD §23).

Uses `opencode run` for non-interactive execution. Output is text, so usage is
harvested defensively from any JSON blob near the end of stdout (OpenCode may
embed a JSON trailer with `usage`/`tokens` depending on provider). Fields are
mapped permissively (input_tokens/promptTokens/...) and never invented; a
message's `cost` / request / tool-call counts are lifted the same way
(issue #17).
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
_COST_KEYS = ("cost", "total_cost_usd", "cost_usd")
_REQUEST_KEYS = ("requests", "num_turns")
_TOOL_KEYS = ("tool_calls", "tool_use_count")
# Token keys keep their historical container precedence (usage, then tokens,
# then the object itself); cost/request keys merge across all three so a
# message info shaped `{"tokens": {...}, "cost": 0.5}` is not dropped.
_MERGED_KEYS = (
    *_INPUT_KEYS,
    *_CACHED_KEYS,
    *_OUTPUT_KEYS,
    *_COST_KEYS,
    *_REQUEST_KEYS,
    *_TOOL_KEYS,
)


def _grab(data: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return None


def _grab_float(data: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def _usage_from_dict(data: dict) -> UsageRecord | None:
    record = UsageRecord(
        input_tokens=_grab(data, _INPUT_KEYS),
        cached_input_tokens=_grab(data, _CACHED_KEYS),
        output_tokens=_grab(data, _OUTPUT_KEYS),
        reported_cost_usd=_grab_float(data, _COST_KEYS),
        requests=_grab(data, _REQUEST_KEYS),
        tool_calls=_grab(data, _TOOL_KEYS),
    )
    token_fields = (record.input_tokens, record.cached_input_tokens, record.output_tokens)
    if all(field is None for field in (*token_fields, record.reported_cost_usd, record.requests, record.tool_calls)):
        return None
    return record


def _usage_from_object(obj: dict) -> UsageRecord | None:
    containers = [c for c in (obj.get("usage"), obj.get("tokens")) if isinstance(c, dict)]
    containers.append(obj)
    merged: dict = {}
    for container in containers:
        for key in _MERGED_KEYS:
            if key not in merged and key in container:
                merged[key] = container[key]
    return _usage_from_dict(merged)


class OpenCodeAdapter(HarnessAdapter):
    name = "opencode"
    binary = "opencode"
    capabilities = HarnessCapabilities(
        model_override=True,
        structured_output=True,
        token_usage=True,
        # `cost` / `total_cost_usd` keys are lifted from output JSON (issue #17).
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
            idx = 0
            while idx < len(tail):
                if tail[idx] != "{":
                    idx += 1
                    continue
                try:
                    obj, end = decoder.raw_decode(tail, idx)
                except Exception:
                    idx += 1
                    continue
                if isinstance(obj, dict):
                    parsed = _usage_from_object(obj)
                    if parsed is not None:
                        usage = parsed
                # Skip past the decoded object: nested blobs belong to it and
                # must not overwrite its (richer) merged record.
                idx = end
        except Exception:
            return HarnessResult()
        if usage is None:
            return HarnessResult()
        return HarnessResult(usage=usage)
