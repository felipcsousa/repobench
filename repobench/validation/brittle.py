"""Brittle-assertion linter for verifier diffs (issue #19).

A pure-text heuristic over the ADDED lines of a unified diff: exact-string
assertions (`assert x == "..."`, `assertEqual(x, "...")`, `assertTrue(x ==
"...")`) tend to over-fit the gold solution and reject correct alternatives.
Deliberately a linter, not a parser — no AST, no execution — so it is
false-positive-prone by design and its findings feed Health WARNINGS, never
scores (issue #19).
"""

from __future__ import annotations

import re

# Short literals ("", "ok") are usually meaningful sentinels, not over-fitting.
MIN_LITERAL_LEN = 4
# Findings collected across a build are capped; they only point at files.
BRITTLE_FINDINGS_CAP = 10

# `assert ... == "literal"` — the \w* suffix also covers assertTrue/assertNotEqual
# forms (plain \bassert\b would not match inside them). Non-greedy up to the
# comparison so the literal tested is the one after `==`.
_EXACT_EQ = re.compile(r"""\bassert\w*.*?==\s*(['"])([^'"]{4,})\1""")
# unittest style: assertEqual / assertNotEqual (with or without self.)
_ASSERT_EQUAL = re.compile(r"""\bassert(?:Not)?Equal\s*\([^'"]*['"]([^'"]{4,})['"]""")

_BRITTLE_PATTERNS = (_EXACT_EQ, _ASSERT_EQUAL)


def brittle_assertions(verifier_diff_text: str) -> list[str]:
    """Scan ADDED lines of a unified diff for exact-string assertion smells.

    Returns short human-readable findings like
    `tests/test_x.py: assert result == "Payment successful"` (the file comes
    from the hunk's `+++ b/` header; added lines before any header are
    ignored). Order follows the diff; identical findings are reported once.
    """
    findings: list[str] = []
    current_file: str | None = None
    for line in verifier_diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            # /dev/null appears on the +++ side of deletions — nothing added there.
            current_file = _drop_b_prefix(target) if target != "/dev/null" else None
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current_file is None:
            continue
        content = line[1:].strip()
        if not any(rx.search(content) for rx in _BRITTLE_PATTERNS):
            continue
        finding = f"{current_file}: {content}"
        if finding not in findings:
            findings.append(finding)
    return findings


def brittle_file_warnings(findings: list[str]) -> list[str]:
    """Collapse scan findings into one warning per distinct file (issue #19).

    The heuristic is false-positive-prone, so a warning only points at the
    file; the findings themselves are never a score input.
    """
    warnings: list[str] = []
    for finding in findings:
        path = finding.split(":", 1)[0]
        warning = f"brittle exact-string assertions in {path}"
        if warning not in warnings:
            warnings.append(warning)
    return warnings


def _drop_b_prefix(path: str) -> str:
    return path[2:] if path.startswith("b/") else path
