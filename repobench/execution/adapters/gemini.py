"""Gemini CLI adapter (PRD §24).

Headless execution via `-o json -p <prompt>`; usage comes from `usageMetadata`
in the JSON response (promptTokenCount / candidatesTokenCount / thoughtsTokenCount).
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


def _usage_from_metadata(metadata: dict) -> UsageRecord | None:
    input_tokens = _int_field(metadata, "promptTokenCount")
    output_tokens = _int_field(metadata, "candidatesTokenCount")
    reasoning_tokens = _int_field(metadata, "thoughtsTokenCount")
    cached = _int_field(metadata, "cachedContentTokenCount")
    if input_tokens is None and output_tokens is None and reasoning_tokens is None and cached is None:
        return None
    return UsageRecord(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
    )


class GeminiAdapter(HarnessAdapter):
    name = "gemini"
    binary = "gemini"
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
        argv = ["gemini"]
        if target.model:
            argv += ["--model", target.model]
        argv += ["-o", "json"]
        argv += list(target.args)
        argv += ["-p", prompt]
        return CommandSpec(
            argv=argv,
            cwd=workspace,
            env={},
            timeout_seconds=timeout_seconds,
            output_mode=OutputMode.JSON,
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        try:
            data = json.loads(stdout or "")
        except Exception:
            return HarnessResult()
        candidates: list[dict] = []
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list):
            candidates.extend(item for item in reversed(data) if isinstance(item, dict))
        for item in candidates:
            metadata = item.get("usageMetadata")
            if not isinstance(metadata, dict):
                continue
            usage = _usage_from_metadata(metadata)
            if usage is not None:
                return HarnessResult(usage=usage)
        return HarnessResult()
