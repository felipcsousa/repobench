"""GitHub API operations via the ``gh`` CLI."""

from __future__ import annotations

import json
from datetime import datetime

from repobench.logging import get_logger
from repobench.models import PullRequest
from repobench.utils import run_cmd_safe

log = get_logger("repository.github")


def is_authenticated() -> bool:
    """Check if the user is authenticated with ``gh``."""
    success, stdout, _ = run_cmd_safe(["gh", "auth", "status"])
    return success and "Logged in" in stdout


def fetch_merged_prs(owner: str, repo: str, lookback_days: int = 180) -> list[PullRequest]:
    """Fetch merged PRs from GitHub API via ``gh``.

    Uses GraphQL for efficiency, falling back to REST if needed.
    """
    query = f"repo:{owner}/{repo} is:pr is:merged merged:>={_date_n_days_ago(lookback_days)}"
    success, stdout, stderr = run_cmd_safe(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "merged",
            "--json",
            "number,title,body,author,labels,mergedAt,mergeCommit,"
            "additions,deletions,changedFiles,headRefOid,baseRefName,"
            "headRefName,url",
            "--limit",
            "1000",
            "--jq",
            ".[]",
        ],
        timeout=600,
    )

    # gh pr list doesn't have --jq directly like that; use --json and parse
    success, stdout, stderr = run_cmd_safe(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "merged",
            "--json",
            "number,title,body,author,labels,mergedAt,mergeCommit,"
            "additions,deletions,changedFiles,headRefOid,baseRefName,"
            "headRefName,url",
            "--limit",
            "1000",
        ],
        timeout=600,
    )
    if not success:
        log.error("Failed to fetch PRs: %s", stderr)
        return []

    try:
        pr_list = json.loads(stdout)
    except json.JSONDecodeError:
        log.error("Failed to parse PR list JSON")
        return []

    prs: list[PullRequest] = []
    cutoff = datetime.now(UTC).replace(tzinfo=None)
    from datetime import timedelta

    cutoff = cutoff - timedelta(days=lookback_days)

    for item in pr_list:
        merged_at_str = item.get("mergedAt")
        if not merged_at_str:
            continue
        merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        if merged_at < cutoff:
            continue

        labels = [l.get("name", "") for l in (item.get("labels") or [])]
        author = item.get("author", {})
        author_login = author.get("login", "") if isinstance(author, dict) else str(author)
        author_type = author.get("type", "") if isinstance(author, dict) else ""

        # Extract linked issue from body
        linked_issue_num, linked_issue_body = _extract_linked_issue(item.get("body", ""))

        merge_commit = item.get("mergeCommit") or {}
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None

        # Get changed files via gh pr diff
        changed_files = _get_pr_files(owner, repo, item["number"])

        pr = PullRequest(
            pr_number=item["number"],
            title=item.get("title", ""),
            body=item.get("body"),
            author=author_login,
            author_type=author_type,
            labels=labels,
            merged_at=merged_at,
            merge_sha=merge_sha,
            merge_commit_sha=merge_sha,
            head_commit_sha=item.get("headRefOid"),
            changed_files=changed_files,
            additions=item.get("additions", 0),
            deletions=item.get("deletions", 0),
            linked_issue_number=linked_issue_num,
            linked_issue_body=linked_issue_body,
            diff_url=item.get("url"),
        )
        prs.append(pr)

    log.info("Fetched %d merged PRs from GitHub (%s/%s)", len(prs), owner, repo)
    return prs


def fetch_pr_details(owner: str, repo: str, pr_number: int) -> PullRequest | None:
    """Fetch detailed PR information including issue linkage."""
    success, stdout, stderr = run_cmd_safe(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,title,body,author,labels,mergedAt,mergeCommit,"
            "additions,deletions,changedFiles,headRefOid,baseRefName,"
            "headRefName,url",
        ],
    )
    if not success:
        log.error("Failed to fetch PR #%d: %s", pr_number, stderr)
        return None

    try:
        item = json.loads(stdout)
    except json.JSONDecodeError:
        log.error("Failed to parse PR details JSON")
        return None

    merged_at_str = item.get("mergedAt")
    merged_at = None
    if merged_at_str:
        merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00")).replace(
            tzinfo=None
        )

    labels = [l.get("name", "") for l in (item.get("labels") or [])]
    author = item.get("author", {})
    author_login = author.get("login", "") if isinstance(author, dict) else str(author)
    author_type = author.get("type", "") if isinstance(author, dict) else ""

    linked_issue_num, linked_issue_body = _extract_linked_issue(item.get("body", ""))

    # Try to get linked issue details
    if linked_issue_num:
        issue_body, issue_created = _fetch_issue_details(owner, repo, linked_issue_num)
        if linked_issue_body is None:
            linked_issue_body = issue_body
    else:
        issue_created = None

    merge_commit = item.get("mergeCommit") or {}
    merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None

    changed_files = _get_pr_files(owner, repo, pr_number)

    return PullRequest(
        pr_number=pr_number,
        title=item.get("title", ""),
        body=item.get("body"),
        author=author_login,
        author_type=author_type,
        labels=labels,
        merged_at=merged_at,
        merge_sha=merge_sha,
        merge_commit_sha=merge_sha,
        head_commit_sha=item.get("headRefOid"),
        changed_files=changed_files,
        additions=item.get("additions", 0),
        deletions=item.get("deletions", 0),
        linked_issue_number=linked_issue_num,
        linked_issue_body=linked_issue_body,
        linked_issue_created_at=issue_created,
        diff_url=item.get("url"),
    )


def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR."""
    success, stdout, stderr = run_cmd_safe(
        ["gh", "pr", "diff", str(pr_number), "--repo", f"{owner}/{repo}"],
    )
    if not success:
        log.error("Failed to fetch diff for PR #%d: %s", pr_number, stderr)
        return ""
    return stdout


def _get_pr_files(owner: str, repo: str, pr_number: int) -> list[str]:
    """Get list of changed files in a PR."""
    success, stdout, _ = run_cmd_safe(
        [
            "gh",
            "pr",
            "diff",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--name-only",
        ],
    )
    if not success:
        # Fallback: parse diff for filenames
        return _get_files_from_diff(owner, repo, pr_number)

    files = [f.strip() for f in stdout.strip().splitlines() if f.strip()]
    return files


def _get_files_from_diff(owner: str, repo: str, pr_number: int) -> list[str]:
    """Extract filenames from the diff output."""
    diff = fetch_pr_diff(owner, repo, pr_number)
    import re

    files = set()
    for match in re.finditer(r"^diff --git a/(.*?) b/", diff, re.MULTILINE):
        files.add(match.group(1))
    return sorted(files)


def _extract_linked_issue(body: str | None) -> tuple[int | None, str | None]:
    """Extract linked issue number and body from PR body text."""
    import re

    if not body:
        return None, None

    # Common patterns: "Fixes #123", "Closes #123", "Resolves #123"
    m = re.search(r"(?:Fixes|Closes|Resolves|fixes|closes|resolves)\s+#(\d+)", body)
    if m:
        return int(m.group(1)), body

    return None, body


def _fetch_issue_details(
    owner: str, repo: str, issue_number: int
) -> tuple[str | None, datetime | None]:
    """Fetch issue body and creation date."""
    success, stdout, _ = run_cmd_safe(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "body,createdAt",
        ],
    )
    if not success:
        return None, None

    try:
        data = json.loads(stdout)
        created_at = None
        if data.get("createdAt"):
            created_at = datetime.fromisoformat(data["createdAt"].replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        return data.get("body"), created_at
    except (json.JSONDecodeError, ValueError):
        return None, None


def _date_n_days_ago(days: int) -> str:
    """Return an ISO date string for N days ago."""
    from datetime import timedelta

    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%d")
