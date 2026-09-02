"""Minimal Git plumbing shared by repository mining and task reconstruction.

Argv-only (no shell). Commands run against an existing repository work tree.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from repobench.core.types import ProcessResult


def git_run(repo: Path, *args: str, timeout_seconds: int = 300) -> ProcessResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ProcessResult(exit_code=None, timed_out=True, duration_ms=int((time.monotonic() - start) * 1000))
    return ProcessResult(
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - start) * 1000),
        stdout=proc.stdout.decode("utf-8", "replace"),
        stderr=proc.stderr.decode("utf-8", "replace"),
    )


def rev_parse(repo: Path, rev: str) -> str | None:
    r = git_run(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return r.stdout.strip() or None if r.exit_code == 0 else None


# Well-known hash of git's empty tree object (issue #33): the root tree of every
# repository's initial commit before the first file lands on its parent side.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def tree_is_empty(repo: Path, sha: str) -> bool:
    """True when `sha` resolves to a commit whose root tree is the empty tree —
    e.g. a repository's initial commit, the merge base of a PR that adds the
    whole repository (issue #33)."""
    r = git_run(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}")
    return r.exit_code == 0 and r.stdout.strip() == EMPTY_TREE_SHA


def archive_commit(repo: Path, sha: str, out_tar: Path) -> bool:
    """`git archive` a commit into a tar file — the workspace materialization path (PRD §33)."""
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_tar.open("wb") as fh:
            proc = subprocess.run(
                ["git", "archive", "--format=tar", sha],
                cwd=str(repo),
                stdout=fh,
                stderr=subprocess.PIPE,
                timeout=600,
            )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def diff_commits(repo: Path, a: str, b: str) -> str:
    r = git_run(repo, "diff", f"{a}..{b}")
    return r.stdout if r.exit_code == 0 else ""


def numstat(repo: Path, a: str, b: str) -> list[tuple[int, int, str]]:
    """Per-file (added, removed, path). Binary files report -1/-1."""
    r = git_run(repo, "diff", "--numstat", f"{a}..{b}")
    out: list[tuple[int, int, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add_s, rem_s, path = parts
        try:
            add = int(add_s) if add_s != "-" else -1
            rem = int(rem_s) if rem_s != "-" else -1
        except ValueError:
            continue
        out.append((add, rem, path))
    return out
