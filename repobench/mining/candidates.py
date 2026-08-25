"""Candidate discovery and filtering from repository history."""

from __future__ import annotations

from repobench.config import RepoBenchConfig
from repobench.logging import get_logger
from repobench.models import (
    CandidateTask,
    PRWorkloadInfo,
    PullRequest,
    RejectionReason,
    TaskStatus,
    TaskType,
)

log = get_logger("mining.candidates")


def discover_candidates(
    prs: list[PRWorkloadInfo],
    config: RepoBenchConfig,
) -> list[CandidateTask]:
    """Transform workload info entries into CandidateTasks.

    Applies initial filters based on the task mining configuration.
    """
    candidates: list[CandidateTask] = []
    tm = config.task_mining

    for info in prs:
        pr = info.pr
        candidate = CandidateTask(
            pr_number=pr.pr_number,
            pr_title=pr.title,
            base_sha=pr.base_sha or "",
            gold_sha=pr.head_sha or pr.merge_sha or "",
            merge_commit_sha=pr.merge_commit_sha,
            head_commit_sha=pr.head_commit_sha,
            task_type=info.task_type,
            task_type_confidence=info.task_type_confidence,
            subsystem=info.subsystem,
            complexity=info.complexity,
            implementation_loc=info.implementation_loc,
            implementation_files=info.implementation_files,
            test_loc=info.test_loc,
            test_files=info.test_files,
            status=TaskStatus.DISCOVERED,
        )

        # Set initial eligibility signals
        candidate.eligibility.history = bool(pr.base_sha and pr.head_sha)

        # Check test change requirement
        if tm.require_test_change and info.test_files == 0:
            candidate.rejection_reason = RejectionReason.NO_TEST_CHANGE
            candidate.status = TaskStatus.FILTERED
        else:
            candidate.eligibility.verifier = info.test_files > 0

        # Check size bounds
        if info.implementation_loc > tm.max_implementation_loc:
            candidate.rejection_reason = RejectionReason.TASK_TOO_LARGE
            candidate.status = TaskStatus.FILTERED
        elif info.implementation_loc < tm.min_implementation_loc:
            candidate.rejection_reason = RejectionReason.TASK_TOO_SMALL
            candidate.status = TaskStatus.FILTERED

        # Check file count bounds
        if info.implementation_files > tm.max_implementation_files:
            candidate.rejection_reason = RejectionReason.TASK_TOO_LARGE
            candidate.status = TaskStatus.FILTERED

        # Check supported types
        if info.task_type.value not in tm.supported_types and info.task_type != TaskType.UNKNOWN:
            candidate.rejection_reason = RejectionReason.NO_INSTRUCTION
            candidate.status = TaskStatus.FILTERED

        # Filter automated maintenance PRs
        if _is_automated_maintenance(pr):
            candidate.rejection_reason = RejectionReason.NO_INSTRUCTION
            candidate.status = TaskStatus.FILTERED

        # Filter documentation-only PRs
        if _is_docs_only(pr):
            candidate.rejection_reason = RejectionReason.NO_INSTRUCTION
            candidate.status = TaskStatus.FILTERED

        # Filter oversized PRs (file count)
        if len(pr.changed_files) > tm.max_implementation_files * 2:
            candidate.rejection_reason = RejectionReason.TASK_TOO_LARGE
            candidate.status = TaskStatus.FILTERED

        # Mark history-supported candidates
        if candidate.status == TaskStatus.DISCOVERED:
            if not pr.base_sha or not pr.head_sha:
                candidate.rejection_reason = RejectionReason.HISTORY_UNSUPPORTED
                candidate.status = TaskStatus.FILTERED
            else:
                candidate.eligibility.history = True

        candidates.append(candidate)

    discovered = sum(1 for c in candidates if c.status == TaskStatus.DISCOVERED)
    filtered = sum(1 for c in candidates if c.status == TaskStatus.FILTERED)
    log.info(
        "Discovery: %d total PRs -> %d discovered, %d filtered", len(prs), discovered, filtered
    )

    return candidates


def filter_candidates(
    candidates: list[CandidateTask],
    config: RepoBenchConfig,
) -> list[CandidateTask]:
    """Apply additional filters to narrow down candidates.

    This is the second pass after initial discovery. Applies stricter
    eligibility checks.
    """
    for candidate in candidates:
        if candidate.status != TaskStatus.DISCOVERED:
            continue

        # Already rejected by size/type in discover - skip
        if candidate.rejection_reason is not None:
            continue

        # Verify instruction can be extracted
        if not candidate.pr_title:
            candidate.rejection_reason = RejectionReason.NO_INSTRUCTION
            candidate.status = TaskStatus.FILTERED
            continue

        # Mark instruction eligibility
        candidate.eligibility.instruction = bool(candidate.pr_title)
        candidate.instruction_source = "pr_title"

        # If we have a linked issue, that's better
        # (will be enriched later by instruction extraction)
        if candidate.instruction_source:
            candidate.eligibility.instruction = True

    valid = sum(1 for c in candidates if c.status == TaskStatus.DISCOVERED)
    log.info("Filter pass: %d candidates remaining as discovered", valid)

    return candidates


def _is_automated_maintenance(pr: PullRequest) -> bool:
    """Check if a PR is automated maintenance."""
    title_lower = pr.title.lower()
    body_lower = (pr.body or "").lower()

    # Dependabot
    if "dependabot" in (pr.author or "").lower():
        return True
    if "dependabot" in title_lower:
        return True
    if "[dependabot" in title_lower:
        return True

    # Renovate
    if "renovate" in (pr.author or "").lower():
        return True
    if "renovate" in title_lower:
        return True

    # Version bumps
    version_patterns = [
        r"^bump\s",
        r"^chore.*bump",
        r"^chore.*version",
        r"^update.*version",
        r"^v?\d+\.\d+\.\d+$",
    ]
    import re

    for pattern in version_patterns:
        if re.match(pattern, title_lower.strip()):
            return True

    # Lockfile-only changes
    lockfiles = {
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Gemfile.lock",
        "Cargo.lock",
        "go.sum",
    }
    if pr.changed_files:
        non_lock = [f for f in pr.changed_files if f not in lockfiles]
        if not non_lock:
            return True

    # Bot author types
    if pr.author_type in ("Bot", "app"):
        bot_keywords = ["dependabot", "renovate", "github-actions", "codecov"]
        for kw in bot_keywords:
            if kw in (pr.author or "").lower():
                return True

    return False


def _is_docs_only(pr: PullRequest) -> bool:
    """Check if a PR only changes documentation."""
    if not pr.changed_files:
        return False

    doc_extensions = {".md", ".mdx", ".rst", ".txt"}
    doc_dirs = {
        "docs",
        "doc",
        "documentation",
        ".github/ISSUE_TEMPLATE",
        ".github/PULL_REQUEST_TEMPLATE",
    }

    all_doc = True
    for f in pr.changed_files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        is_doc_ext = ext in doc_extensions
        is_doc_dir = any(f.startswith(d) for d in doc_dirs)
        if not is_doc_ext and not is_doc_dir:
            all_doc = False
            break

    return all_doc
