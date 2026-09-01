"""Harness adapter registry (PRD §18-19).

Maps harness names to their adapters. Official Tier-1 harnesses (claude, codex,
opencode, gemini) plus the Tier-2 generic command adapter.
"""

from __future__ import annotations

from repobench.core.errors import UsageError
from repobench.execution.adapters.base import HarnessAdapter
from repobench.execution.adapters.claude import ClaudeAdapter
from repobench.execution.adapters.codex import CodexAdapter
from repobench.execution.adapters.command import CommandAdapter
from repobench.execution.adapters.gemini import GeminiAdapter
from repobench.execution.adapters.opencode import OpenCodeAdapter

KNOWN_HARNESSES: tuple[str, ...] = ("claude", "codex", "opencode", "gemini", "command")

_ADAPTER_CLASSES: tuple[type[HarnessAdapter], ...] = (
    ClaudeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    GeminiAdapter,
    CommandAdapter,
)


def all_adapters() -> dict[str, HarnessAdapter]:
    """Fresh adapter instances keyed by harness name, in KNOWN_HARNESSES order."""
    return {cls.name: cls() for cls in _ADAPTER_CLASSES}


def get_adapter(harness: str) -> HarnessAdapter:
    key = (harness or "").strip().lower()
    adapters = all_adapters()
    if key not in adapters:
        raise UsageError(
            f"unknown harness: {harness!r} (known harnesses: {', '.join(KNOWN_HARNESSES)})"
        )
    return adapters[key]
