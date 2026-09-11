"""Shared rendering plumbing (PRD §90): the Rich console, the echo/fail/kv
primitives and the OK/MISS/WARN marks.

All dynamic content is printed with markup disabled — repository paths and
task ids may contain characters Rich would otherwise interpret.
"""

from __future__ import annotations

import sys

from rich.console import Console

# Never truncate data on a pipe: terminals get their real width, captured output
# (tests, CI) gets plenty of room so table cells are never ellipsized.
console = Console(width=None if sys.stdout.isatty() else 200)

OK = "✓"
MISS = "✗"
WARN = "⚠"


def echo(message: str = "", flush: bool = False) -> None:
    console.print(message, markup=False, highlight=False, soft_wrap=True)
    # Piped stdout is block-buffered, so long-running steps must flush after
    # each line or the user stares at silence until the process exits.
    if flush:
        sys.stdout.flush()


def fail(message: str) -> None:
    echo(f"error: {message}")


def kv(label: str, value: str) -> None:
    # The explicit space keeps long labels (>= 28 cols) from abutting the value;
    # for shorter labels the padding absorbs it and the column is unchanged.
    echo(f"{label:<27} {value}")
