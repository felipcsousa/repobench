"""Leakage v2 risk-class tests (PRD §44-46).

The detector must distinguish GENERIC issue/PR references ("#7") from EXACT
gold identifiers — the new function/class/symbol names the gold patch
introduces (PRD §44). Only HIGH hard-rejects; MEDIUM is a candidate warning
and LOW is clean (PRD §45); every classification carries evidence naming the
file and the reason (PRD §46). The numeric score is informational — the gate
reads `risk`, never the score.
"""

from __future__ import annotations

import sys
from pathlib import Path

from repobench.config import ProjectConfig
from repobench.core import gitutil
from repobench.core.types import RejectionCode, TaskStatus
from repobench.tasks.leakage import LeakageReport, scan_base_archive
from repobench.tasks.reconstruction import build_task_package
from repobench.validation.pipeline import TaskValidator
from tests.fixtures.gitutil import build_repo, git, make_candidate

# Quoted interpreter: commands go through shlex.split (POSIX mode) — same
# pattern as tests/test_validation.py.
PYTEST_CMD = f'"{sys.executable}" -m pytest -q'

# The PRD §46 example: GOLD introduces `class IdempotencyRegistry`.
IDEMPOTENCY_MODULE = "class IdempotencyRegistry:\n    pass\n"


def make_task(tmp_path: Path, **repo_kwargs):
    """Task package from the shared buggy-calculator fixture (PR #9)."""
    fx = build_repo(tmp_path, **repo_kwargs)
    return build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")


def make_project() -> ProjectConfig:
    return ProjectConfig(test_command=PYTEST_CMD, regression_command=None)


def by_name(report) -> dict:
    return {check.name: check for check in report.checks}


# ------------------------------------------------------------- needle classes


def test_generic_pr_reference_is_medium_with_evidence(tmp_path: Path) -> None:
    # PRD §44's exact false-positive class: "#9" in a base file is a generic
    # issue/PR reference — MEDIUM, never a reject.
    task = make_task(tmp_path, readme="# calc\n\nSee #9 for the original discussion.\n")
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "MEDIUM"
    assert len(report.evidence) == 1
    assert "README.md" in report.evidence[0]
    assert "'#9'" in report.evidence[0]
    assert report.findings == report.evidence  # backward-compat alias


def test_pull_reference_forms_are_medium(tmp_path: Path) -> None:
    task = make_task(tmp_path, readme="# calc\n\nBackported from pull/9 and pulls/9.\n")
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "MEDIUM"
    assert len(report.evidence) == 2
    assert any("'pull/9'" in line for line in report.evidence)
    assert any("'pulls/9'" in line for line in report.evidence)


def test_gold_sha_prefix_is_high_with_file_evidence(tmp_path: Path) -> None:
    # HIGH needle 1 (PRD §45): the gold commit SHA prefix inside the base
    # archive; evidence names the file (PRD §46). A generic "#9" in the same
    # file adds MEDIUM evidence without changing the verdict.
    fx = build_repo(tmp_path)
    package = build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")
    leaked_sha = "cafe1234cafe1234cafe1234cafe1234cafe1234"
    (fx["repo"] / "NOTES.txt").write_text(
        "internal build id: cafe1234cafe1234 — see #9\n"
    )
    git(fx["repo"], "add", "-A")
    git(fx["repo"], "commit", "--quiet", "--no-gpg-sign", "-m", "add notes")
    notes_base = git(fx["repo"], "rev-parse", "HEAD")
    assert gitutil.archive_commit(fx["repo"], notes_base, tmp_path / "leaky.tar")

    metadata = package.metadata.model_copy(update={"gold_sha": leaked_sha})
    report = scan_base_archive(metadata, tmp_path / "leaky.tar", gold_patch=None)
    assert report.risk == "HIGH"  # MEDIUM evidence never downgrades a HIGH
    assert report.evidence == [
        "generic PR reference '#9' found in base archive file 'NOTES.txt' "
        "(ordinary issue/PR cross-reference, not a gold identifier — PRD §44)",
        "gold SHA prefix 'cafe1234cafe' found in base archive file 'NOTES.txt'",
    ]
    assert report.score == 78 - 40 - 10  # informational penalties only


def test_gold_identifier_in_base_is_high(tmp_path: Path) -> None:
    # HIGH needle 2 (PRD §45) and the PRD §46 example: the gold patch
    # introduces `class IdempotencyRegistry` and the base archive already
    # mentions the symbol — the base leaks part of the solution naming.
    task = make_task(
        tmp_path,
        readme="# calc\n\nTODO: wire the IdempotencyRegistry in here.\n",
        extra_files={"idempotency.py": IDEMPOTENCY_MODULE},
    )
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "HIGH"
    assert len(report.evidence) == 1
    assert "gold identifier 'IdempotencyRegistry'" in report.evidence[0]
    assert "introduced in GOLD" in report.evidence[0]
    assert "README.md" in report.evidence[0]
    assert report.score == 78 - 40


def test_short_identifier_definition_is_filtered(tmp_path: Path) -> None:
    # "parse" is 5 chars — below the 6-char minimum, so a gold `def parse` is
    # never treated as a gold identifier even when the base mentions it. (A
    # name like "helper" is already 6 chars and would be extracted.)
    task = make_task(
        tmp_path,
        readme="# calc\n\nWe parse this format elsewhere.\n",
        extra_files={"util.py": "def parse():\n    return 1\n"},
    )
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "LOW"
    assert report.evidence == []


def test_clean_base_is_low_with_empty_evidence(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "LOW"
    assert report.evidence == []
    assert report.score == 78  # 100 - 22 network penalty only (informational)
    assert report.checks["network_isolation"] is False  # honesty: never claimed


def test_gold_patch_none_still_scans_sha_and_generic_refs(tmp_path: Path) -> None:
    # gold_patch=None keeps working (identifier extraction skipped): the base
    # mentions IdempotencyRegistry AND carries a generic "#9", but without the
    # gold patch only the generic ref is classifiable — MEDIUM, no accusation
    # invented from an unknown symbol.
    task = make_task(
        tmp_path,
        readme="# calc\n\nSee #9. TODO: wire the IdempotencyRegistry in here.\n",
        extra_files={"idempotency.py": IDEMPOTENCY_MODULE},
    )
    report = scan_base_archive(task.metadata, task.base_tar)
    assert report.risk == "MEDIUM"
    assert len(report.evidence) == 1
    assert "'#9'" in report.evidence[0]
    assert all("IdempotencyRegistry" not in line for line in report.evidence)


# ----------------------------------------------------------------- the gate


def test_high_risk_rejects_at_the_gate_before_any_command(tmp_path: Path) -> None:
    # PRD §45: only HIGH is a hard reject — and it short-circuits before the
    # validator runs a single command (only the leakage check is present).
    task = make_task(tmp_path)
    report = LeakageReport(
        checks={},
        score=78,
        evidence=[
            "gold SHA prefix 'cafe1234cafe' found in base archive file 'NOTES.txt'"
        ],
        risk="HIGH",
    )
    result = TaskValidator(ProjectConfig(), workspaces_root=tmp_path / "ws").validate(
        task, leakages=report
    )
    assert result.status is TaskStatus.REJECTED
    assert result.rejection_code is RejectionCode.LEAKAGE_HIGH
    assert [check.name for check in result.checks] == ["leakage"]


def test_medium_risk_score_is_informational_and_gate_passes(tmp_path: Path) -> None:
    # PRD §46: evidence over opaque scores — a MEDIUM report carries a real
    # score penalty yet must not reject at the pipeline level.
    task = make_task(tmp_path, readme="# calc\n\nSee #9 for the original discussion.\n")
    report = scan_base_archive(task.metadata, task.base_tar, gold_patch=task.gold_patch)
    assert report.risk == "MEDIUM"
    assert report.score < 100

    result = TaskValidator(make_project(), workspaces_root=tmp_path / "ws").validate(
        task, leakages=report
    )
    assert result.status is TaskStatus.VALID
    leakage_check = by_name(result)["leakage"]
    assert leakage_check.passed is True
    assert "MEDIUM" in leakage_check.details  # the warning is visible, not silent
    assert "#9" in leakage_check.details
