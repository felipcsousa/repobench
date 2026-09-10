"""Solution leakage scan of the base archive (PRD §87, §44-46).

Scans every decodable text file in base.tar and classifies the result into a
risk level (PRD §45):

- HIGH — exact gold material: the gold commit SHA prefix, or an identifier the
  gold patch introduces (a "new function/class/symbol", PRD §44) occurring in
  the base text. Hard reject.
- MEDIUM — generic issue/PR references ("#7", "pull/7", "pulls/7"): ordinary
  cross-referencing that naive substring matching used to reject wrongly
  (PRD §44). Candidate warning only, never a reject.
- LOW — nothing found. Clean.

Every classification carries evidence saying why (PRD §46); the numeric score
is informational — the validation gate is `risk`, not the score. Structural
isolation checks are true by construction; network isolation is explicitly
false in host-native V1 (PRD §87) instead of faking security.

Honesty note (PRD §53-54): the gold-identifier needle matches the SYMBOL NAME
only. A base file mentioning the name (docs, TODOs, issue text) leaks part of
the solution naming, never its body — the evidence string says exactly that.
"""

from __future__ import annotations

import re
import tarfile
import tempfile
from pathlib import Path
from typing import Literal

import pydantic

from repobench.core.types import TaskMetadata

MAX_SCAN_BYTES = 2 * 1024 * 1024  # skip files >= 2MB
NETWORK_PENALTY = 22  # host-native V1 has no network sandbox (PRD §87)
HIGH_PENALTY = 40  # informational-score penalty per HIGH evidence item
MEDIUM_PENALTY = 10  # informational-score penalty per MEDIUM evidence item

CHECK_KEYS = (
    "history_isolation",
    "gold_isolation",
    "verifier_isolation",
    "github_credentials",
    "network_isolation",
)

# Gold-introduced definition forms (PRD §44 "new function/class/symbol"): one
# regex over the common definition keywords across Python/TS/Go/Rust-style
# languages; the captured group is the defined identifier.
_DEFINITION_RE = re.compile(
    r"\b(?:def|class|function|fn|func|interface|type|struct|enum|const|let|var)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)"
)
MIN_IDENTIFIER_LENGTH = 6  # shorter names are too generic to accuse (PRD §44)

# Common words that appear as definitions everywhere; a gold patch defining
# one of them says nothing about leakage. Matched case-insensitively.
STOPWORDS = frozenset(
    {
        "default",
        "exports",
        "module",
        "require",
        "return",
        "string",
        "number",
        "boolean",
        "object",
        "function",
        "interface",
        "window",
        "document",
        "process",
        "constructor",
        "prototype",
    }
)


class LeakageReport(pydantic.BaseModel):
    """Leakage scan result (PRD §45-46).

    `risk` is the verdict — only HIGH hard-rejects (PRD §45); `evidence` says
    why for every classification (PRD §46). `checks` and `score` are kept for
    backward compatibility with persisted data: score is informational
    (100 − network penalty − per-evidence penalties, clamped 0-100) and never
    decides the gate.
    """

    checks: dict[str, bool]  # keys: history_isolation, gold_isolation, verifier_isolation,
    # github_credentials, network_isolation
    score: int  # 0-100, informational — the GATE is `risk`, not the score
    evidence: list[str]
    risk: Literal["LOW", "MEDIUM", "HIGH"]


def _gold_identifiers(gold_patch: Path) -> set[str]:
    """Identifiers the gold patch introduces (PRD §44): definitions on added
    lines ("+", excluding "+++"), filtered to length >= MIN_IDENTIFIER_LENGTH
    and outside STOPWORDS. An unreadable gold patch yields no identifiers —
    the scan degrades to the SHA/generic-ref needles instead of inventing a
    leak (PRD §53-54)."""
    try:
        text = gold_patch.read_text(errors="replace")
    except OSError:
        return set()
    identifiers: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for match in _DEFINITION_RE.finditer(line):
            name = match.group(1)
            if len(name) >= MIN_IDENTIFIER_LENGTH and name.lower() not in STOPWORDS:
                identifiers.add(name)
    return identifiers


def scan_base_archive(
    metadata: TaskMetadata,
    base_archive: Path,
    *,
    gold_patch: Path | None = None,
) -> LeakageReport:
    """Scan the base archive for solution leakage and classify the risk
    (PRD §44-46).

    The needles derive from the task metadata and the gold patch:
      HIGH   — the gold commit SHA prefix (first 12 chars of gold_sha), or an
               identifier introduced by the gold patch (when gold_patch is
               given) occurring whole-word in a base file.
      MEDIUM — generic "#<pr_number>" / "pull/<n>" / "pulls/<n>" references
               (PRD §44's false-positive class).

    gold_patch=None keeps working: identifier extraction is skipped and only
    the SHA / generic-ref needles are checked.
    """
    gold_sha = metadata.gold_sha
    pr_number = metadata.pr_number

    sha_needle = gold_sha[:12] if gold_sha else None
    identifiers = _gold_identifiers(gold_patch) if gold_patch is not None else set()
    identifier_patterns = [
        (name, re.compile(rf"\b{re.escape(name)}\b")) for name in sorted(identifiers)
    ]
    generic_needles: list[str] = []
    if pr_number is not None:
        generic_needles.append(f"#{pr_number}")
        generic_needles.append(f"pull/{pr_number}")
        generic_needles.append(f"pulls/{pr_number}")

    high_evidence: list[str] = []
    medium_evidence: list[str] = []
    with tempfile.TemporaryDirectory(prefix="repobench-leak-") as tmp:
        extracted = Path(tmp)
        with tarfile.open(base_archive) as tar:
            tar.extractall(extracted, filter="data")
        for path in sorted(extracted.rglob("*")):
            if not path.is_file():
                continue
            try:
                if path.stat().st_size >= MAX_SCAN_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:  # binary, not a decodable text file
                continue
            text = data.decode("utf-8", errors="replace")
            relpath = path.relative_to(extracted)
            if sha_needle and sha_needle in text:
                high_evidence.append(
                    f"gold SHA prefix '{sha_needle}' found in base archive file '{relpath}'"
                )
            for name, pattern in identifier_patterns:
                if pattern.search(text):
                    high_evidence.append(
                        f"gold identifier '{name}' (introduced in GOLD, absent-by-definition "
                        f"in BASE) referenced in base archive file '{relpath}' — the symbol "
                        "GOLD introduces already appears in BASE, leaking part of the "
                        "solution naming"
                    )
            for needle in generic_needles:
                if needle in text:
                    medium_evidence.append(
                        f"generic PR reference '{needle}' found in base archive file "
                        f"'{relpath}' (ordinary issue/PR cross-reference, not a gold "
                        "identifier — PRD §44)"
                    )

    evidence = sorted(high_evidence + medium_evidence)
    if high_evidence:
        risk: Literal["LOW", "MEDIUM", "HIGH"] = "HIGH"
    elif medium_evidence:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    checks: dict[str, bool] = {
        # Structural isolation holds by construction: base.tar only ever contains
        # BASE tree content, never the gold/verifier patches or harness credentials.
        "history_isolation": True,
        "gold_isolation": True,
        "verifier_isolation": True,
        "github_credentials": True,
        # Host-native execution has no network sandbox (PRD §87) — never claim it.
        "network_isolation": False,
    }
    # Informational only (PRD §46: evidence over opaque scores); the validation
    # gate reads `risk` (PRD §45).
    score = (
        100
        - NETWORK_PENALTY
        - HIGH_PENALTY * len(high_evidence)
        - MEDIUM_PENALTY * len(medium_evidence)
    )
    score = max(0, min(100, score))
    return LeakageReport(checks=checks, score=score, evidence=evidence, risk=risk)
