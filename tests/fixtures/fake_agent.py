#!/usr/bin/env python3
"""Fake coding-agent harness for RepoBench tests (PRD §139-141).

Usage: fake_agent.py <workspace> [mode]

The mode is the second argv element, overridable through the RB_FAKE_MODE
environment variable (default "fix"). It never performs inference and always
exits 0 — exit codes deliberately do not define correctness (PRD §42).

Modes:
  fix     repair the sum_even bug in calculator.py (default)
  noop    change nothing
  commit  fix + `git add -A && git commit` (agent commits must still be captured)
  slow    sleep long enough to exercise the timeout path
  leaker  fix, then probe the synthetic workspace the way a cheating agent
          would (`git log --oneline`, `git remote -v`) and dump the result plus
          every TOKEN-carrying environment variable name to leak_report.txt
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BUG = "x % 2 == 1"
FIX = "x % 2 == 0"
LEAK_REPORT = "leak_report.txt"


def _fix_bug(workspace: Path) -> bool:
    target = workspace / "calculator.py"
    if not target.is_file():
        return False
    text = target.read_text()
    if BUG not in text:
        return False
    target.write_text(text.replace(BUG, FIX))
    return True


def _mode_fix(workspace: Path) -> None:
    _fix_bug(workspace)
    print("fake agent: fixed sum_even")


def _mode_noop(workspace: Path) -> None:
    print("fake agent: looked around, changed nothing")


def _mode_commit(workspace: Path) -> None:
    _fix_bug(workspace)
    identity = ("-c", "user.name=Fake Agent", "-c", "user.email=agent@example.com")
    subprocess.run(["git", *identity, "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", *identity, "commit", "--quiet", "-m", "fix sum_even"],
        cwd=workspace,
        check=True,
    )
    print("fake agent: fixed and committed")


def _mode_slow(workspace: Path) -> None:
    print("fake agent: sleeping")
    time.sleep(120)


def _mode_leaker(workspace: Path) -> None:
    _fix_bug(workspace)
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace, capture_output=True, text=True
    )
    remote = subprocess.run(
        ["git", "remote", "-v"], cwd=workspace, capture_output=True, text=True
    )
    token_keys = sorted(key for key in os.environ if "TOKEN" in key)
    lines = [
        "$ git log --oneline",
        log.stdout,
        "$ git remote -v",
        remote.stdout,
        "token env keys:",
        *token_keys,
    ]
    (workspace / LEAK_REPORT).write_text("\n".join(lines) + "\n")
    print("fake agent: fixed and wrote leak_report.txt")


MODES = {
    "fix": _mode_fix,
    "noop": _mode_noop,
    "commit": _mode_commit,
    "slow": _mode_slow,
    "leaker": _mode_leaker,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: fake_agent.py <workspace> [mode]", file=sys.stderr)
        return 2
    workspace = Path(argv[1])
    mode = os.environ.get("RB_FAKE_MODE") or (argv[2] if len(argv) > 2 else "fix")
    handler = MODES.get(mode)
    if handler is None:
        print(f"fake agent: unknown mode {mode!r}; doing nothing", file=sys.stderr)
        return 0
    handler(workspace)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
