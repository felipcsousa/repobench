"""Solution leakage scan of the base archive (PRD §87).

Scans every decodable text file in base.tar for references that would hand the
solution to the agent: the gold commit SHA prefix and PR-number references.
Structural isolation checks are true by construction; network isolation is
explicitly false in host-native V1 (PRD §87) instead of faking security.
"""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

import pydantic

from repobench.core.types import TaskMetadata

MAX_SCAN_BYTES = 2 * 1024 * 1024  # skip files >= 2MB
NETWORK_PENALTY = 22  # host-native V1 has no network sandbox (PRD §87)
LEAK_PENALTY = 15

CHECK_KEYS = (
    "history_isolation",
    "gold_isolation",
    "verifier_isolation",
    "github_credentials",
    "network_isolation",
)


class LeakageReport(pydantic.BaseModel):
    checks: dict[str, bool]  # keys: history_isolation, gold_isolation, verifier_isolation,
    # github_credentials, network_isolation
    score: int  # 0-100
    findings: list[str]


def scan_base_archive(metadata: TaskMetadata, base_archive: Path) -> LeakageReport:
    """Scan the base archive for solution leakage.

    The needles derive from the task metadata: the gold commit SHA prefix and
    PR-number references.
    """
    gold_sha = metadata.gold_sha
    pr_number = metadata.pr_number

    needles: list[tuple[str, str]] = []
    if gold_sha:
        needles.append((f"gold SHA prefix '{gold_sha[:12]}'", gold_sha[:12]))
    if pr_number is not None:
        needles.append((f"PR reference '#{pr_number}'", f"#{pr_number}"))
        needles.append((f"PR reference 'pull/{pr_number}'", f"pull/{pr_number}"))
        needles.append((f"PR reference 'pulls/{pr_number}'", f"pulls/{pr_number}"))

    findings: list[str] = []
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
            for label, needle in needles:
                if needle in text:
                    findings.append(
                        f"{label} found in base archive file '{path.relative_to(extracted)}'"
                    )

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
    score = 100 - NETWORK_PENALTY - LEAK_PENALTY * len(findings)
    score = max(0, min(100, score))
    return LeakageReport(checks=checks, score=score, findings=findings)
