"""Git operations for repository analysis via subprocess."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from repobench.logging import get_logger
from repobench.models import PullRequest
from repobench.utils import run_cmd_safe

log = get_logger("repository.git")


def get_merged_prs_from_git(repo_root: Path, lookback_days: int = 180) -> list[PullRequest]:
    """Extract merged PRs from git log using merge commit metadata.

    Parses merge commits that follow the GitHub merge-message convention
    (e.g. "Merge pull request #123 from ...") and also squash-merge
    patterns (e.g. "feat: ... (#123)").

    Returns PullRequest objects with basic metadata.  GitHub-specific
    metadata (labels, issue linkage) must be enriched separately.
    """
    since_date = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # --- 1. Find merge commits (GitHub merge) ---
    success, stdout, stderr = run_cmd_safe(
        [
            "git",
            "log",
            f"--since={since_date}",
            "--pretty=format:%H||%an||%aI||%s",
            "--merges",
            "--no-merges",  # will be overridden below
        ],
        cwd=repo_root,
    )
    # Note: --merges already filters to merge commits. --no-merges is not needed.
    # Re-run without --no-merges:
    success, stdout, stderr = run_cmd_safe(
        [
            "git",
            "log",
            f"--since={since_date}",
            "--pretty=format:%H||%an||%aI||%s",
            "--merges",
        ],
        cwd=repo_root,
    )
    merge_commits: list[dict[str, str]] = []
    if success and stdout.strip():
        for line in stdout.strip().splitlines():
            parts = line.split("||", 3)
            if len(parts) == 4:
                merge_commits.append(
                    {
                        "sha": parts[0],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3],
                    }
                )

    prs: list[PullRequest] = []

    for mc in merge_commits:
        msg = mc["message"]
        pr_number = _extract_pr_number_from_merge(msg)
        if pr_number is None:
            continue

        # Determine merge strategy from commit parents
        base_sha, gold_sha = _get_merge_parents(repo_root, mc["sha"])
        if base_sha is None or gold_sha is None:
            continue

        # Get changed files
        changed_files = _get_changed_files(repo_root, base_sha, gold_sha)
        additions, deletions = _get_diff_stats(repo_root, base_sha, gold_sha)

        pr = PullRequest(
            pr_number=pr_number,
            title=msg.split("\n")[0][:200],
            body=None,
            author=mc["author"],
            merged_at=datetime.fromisoformat(mc["date"]),
            merge_commit_sha=mc["sha"],
            base_sha=base_sha,
            head_sha=gold_sha,
            merge_sha=gold_sha,
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
        )
        prs.append(pr)

    # --- 2. Find squash merges (no --merges flag) ---
    squash_prs = _find_squash_merges(repo_root, since_date)
    existing_numbers = {p.pr_number for p in prs}
    for sp in squash_prs:
        if sp.pr_number not in existing_numbers:
            prs.append(sp)

    log.info("Found %d merged PRs from git history (lookback %d days)", len(prs), lookback_days)
    return prs


def _extract_pr_number_from_merge(message: str) -> int | None:
    """Extract PR number from merge commit message."""
    import re

    # GitHub merge: "Merge pull request #123 from ..."
    m = re.search(r"Merge pull request #(\d+)", message)
    if m:
        return int(m.group(1))

    # Squash merge: "feat: something (#123)"
    m = re.search(r"\(#(\d+)\)\s*$", message)
    if m:
        return int(m.group(1))

    return None


def _get_merge_parents(repo_root: Path, merge_sha: str) -> tuple[str | None, str | None]:
    """Get the two parents of a merge commit.

    For a merge commit, parent[0] is the base branch (first-parent) and
    parent[1] is the feature branch tip.  For squash merges there is only
    one parent.
    """
    success, stdout, _ = run_cmd_safe(
        ["git", "cat-file", "-p", merge_sha],
        cwd=repo_root,
    )
    if not success:
        return None, None

    parents = []
    for line in stdout.splitlines():
        if line.startswith("parent "):
            parents.append(line.split(" ", 1)[1])

    if len(parents) >= 2:
        # Merge commit: base = first parent, gold = merge sha
        return parents[0], merge_sha
    elif len(parents) == 1:
        # Squash merge: base = parent, gold = this commit
        return parents[0], merge_sha
    return None, None


def _find_squash_merges(repo_root: Path, since_date: str) -> list[PullRequest]:
    """Find squash merges by scanning commits for PR-number patterns."""
    import re

    success, stdout, _ = run_cmd_safe(
        [
            "git",
            "log",
            f"--since={since_date}",
            "--pretty=format:%H||%an||%aI||%s",
            "--no-merges",
        ],
        cwd=repo_root,
    )
    if not success or not stdout.strip():
        return []

    prs = []
    for line in stdout.strip().splitlines():
        parts = line.split("||", 3)
        if len(parts) != 4:
            continue
        sha, author, date_str, message = parts
        m = re.search(r"\(#(\d+)\)\s*$", message)
        if not m:
            continue
        pr_number = int(m.group(1))

        # Check that this commit is not a child of a merge for the same PR
        # (i.e. it's truly a squash merge, not part of a regular merge)
        changed_files = (
            _get_changed_files(repo_root, f"{sha}~1", sha) if _parent_exists(repo_root, sha) else []
        )
        if not changed_files:
            changed_files = _get_changed_files(repo_root, sha + "^{}", sha)

        additions, deletions = 0, 0
        if changed_files:
            additions, deletions = (
                get_diff_stat(repo_root, f"{sha}~1", sha)
                if _parent_exists(repo_root, sha)
                else (0, 0)
            )

        pr = PullRequest(
            pr_number=pr_number,
            title=message.split("\n")[0][:200],
            author=author,
            merged_at=datetime.fromisoformat(date_str),
            merge_commit_sha=sha,
            base_sha=f"{sha}~1" if _parent_exists(repo_root, sha) else "",
            head_sha=sha,
            merge_sha=sha,
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
        )
        prs.append(pr)

    return prs


def _parent_exists(repo_root: Path, sha: str) -> bool:
    success, _, _ = run_cmd_safe(["git", "rev-parse", f"{sha}~1"], cwd=repo_root)
    return success


def get_diff_stat(repo_root: Path, base_sha: str, head_sha: str) -> tuple[int, int]:
    """Get addition and deletion counts between two SHAs."""
    success, stdout, _ = run_cmd_safe(
        ["git", "diff", "--stat", "--numstat", base_sha, head_sha],
        cwd=repo_root,
    )
    if not success:
        return 0, 0

    total_add = 0
    total_del = 0
    for line in stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                total_add += int(parts[0]) if parts[0] != "-" else 0
                total_del += int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
    return total_add, total_del


def checkout_snapshot(repo_root: Path, sha: str, target_dir: Path) -> bool:
    """Create an archive snapshot of a specific commit into target_dir.

    Uses ``git archive`` to avoid exposing .git history to agents.
    Returns True on success.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_file = target_dir / "snapshot.tar"

    success, _, stderr = run_cmd_safe(
        ["git", "archive", "-o", str(archive_file), sha],
        cwd=repo_root,
    )
    if not success:
        log.error("git archive failed for %s: %s", sha, stderr)
        return False

    # Extract the archive
    success, _, stderr = run_cmd_safe(
        ["tar", "xf", str(archive_file), "-C", str(target_dir)],
        cwd=target_dir,
    )
    if not success:
        log.error("tar extraction failed: %s", stderr)
        return False

    # Clean up archive
    archive_file.unlink(missing_ok=True)
    log.debug("Checked out snapshot %s -> %s", sha[:8], target_dir)
    return True


def get_commit_info(repo_root: Path, sha: str) -> dict[str, str] | None:
    """Get detailed info for a single commit."""
    success, stdout, _ = run_cmd_safe(
        [
            "git",
            "show",
            "--pretty=format:%H||%an||%aI||%s||%b",
            "--stat",
            "--no-patch",
            sha,
        ],
        cwd=repo_root,
    )
    if not success:
        return None

    lines = stdout.strip().splitlines()
    if not lines:
        return None

    parts = lines[0].split("||", 4)
    result: dict[str, str] = {
        "sha": parts[0] if len(parts) > 0 else sha,
        "author": parts[1] if len(parts) > 1 else "",
        "date": parts[2] if len(parts) > 2 else "",
        "subject": parts[3] if len(parts) > 3 else "",
        "body": parts[4] if len(parts) > 4 else "",
    }

    # Parse stat lines
    stat_lines = lines[1:] if len(lines) > 1 else []
    result["files_changed"] = str(len(stat_lines))

    return result


def _get_changed_files(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Get list of files changed between two commits."""
    success, stdout, _ = run_cmd_safe(
        ["git", "diff", "--name-only", base_sha, head_sha],
        cwd=repo_root,
    )
    if not success:
        return []
    return [f for f in stdout.strip().splitlines() if f]
