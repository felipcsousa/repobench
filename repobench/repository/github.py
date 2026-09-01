"""GitHub enrichment via the `gh` CLI (PRD §65: issue, PR title/body, labels, author).

Every call degrades gracefully: any failure (gh missing, not authed, offline, bad JSON)
yields None instead of raising — enrichment is best-effort by design.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from repobench.core.types import IssueInfo, PRInfo, ProcessResult
from repobench.execution.process import run_sync

_GH_TIMEOUT_SECONDS = 30
# baseRefOid/headRefOid are deliberately NOT fetched: for merged PRs they track the
# live branch tips, while the merge commit's parents are the authoritative SHAs the
# mining layer derives everything from (numstat, candidate ids, base.tar). Git is the
# source of truth; gh may only add metadata.
_PR_FIELDS = "number,title,body,labels,author,mergedAt,createdAt"
_ISSUE_FIELDS = "number,title,body,createdAt"

# "Fixes #12", "Closes: #34", "resolves #56", "Fix #7" — GitHub's linking keywords.
_LINKED_ISSUE_RE = re.compile(r"\b(Fixes|Closes|Resolves|Fix)\s*:?\s*#(\d+)", re.IGNORECASE)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_author(data: object) -> tuple[str | None, bool]:
    """Returns (login, is_bot). Bot = login contains "[bot]" or author typename is Bot."""
    if not isinstance(data, dict):
        return None, False
    login = data.get("login")
    login_str = login if isinstance(login, str) and login else None
    is_bot = bool(login_str and "[bot]" in login_str) or data.get("__typename") == "Bot"
    return login_str, bool(is_bot)


class GitHubClient:
    """Thin `gh` CLI wrapper for one "owner/repo" slug. Never raises on fetch failure."""

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def _run(self, argv: list[str]) -> ProcessResult:
        return run_sync(argv, Path.cwd(), timeout_seconds=_GH_TIMEOUT_SECONDS)

    def visibility(self) -> str | None:
        """"PUBLIC" | "PRIVATE" | None (unknown). Never raises (PRD §51)."""
        result = self._run(
            ["gh", "repo", "view", self.slug, "--json", "visibility"]
        )
        if result.exit_code != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        value = data.get("visibility") if isinstance(data, dict) else None
        return str(value).upper() if isinstance(value, str) else None

    def get_pr(self, number: int) -> PRInfo | None:
        result = self._run(
            ["gh", "pr", "view", str(number), "-R", self.slug, "--json", _PR_FIELDS]
        )
        if result.exit_code != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        login, is_bot = _parse_author(data.get("author"))
        return PRInfo(
            number=int(data.get("number") or number),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=[
                label["name"]
                for label in (data.get("labels") or [])
                if isinstance(label, dict) and label.get("name")
            ],
            author=login,
            is_bot=is_bot,
            merged_at=_parse_datetime(data.get("mergedAt")),
            created_at=_parse_datetime(data.get("createdAt")),
        )

    def get_issue(self, number: int) -> IssueInfo | None:
        result = self._run(
            ["gh", "issue", "view", str(number), "-R", self.slug, "--json", _ISSUE_FIELDS]
        )
        if result.exit_code != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return IssueInfo(
            number=int(data.get("number") or number),
            title=data.get("title") or "",
            body=data.get("body") or "",
            created_at=_parse_datetime(data.get("createdAt")),
        )

    @staticmethod
    def find_linked_issue_number(body: str) -> int | None:
        match = _LINKED_ISSUE_RE.search(body or "")
        return int(match.group(2)) if match else None

    def enrich(self, pr: PRInfo) -> PRInfo:
        """Fill PR metadata + linked issue from GitHub. Returns the input PR on any failure."""
        try:
            fetched = self.get_pr(pr.number)
            if fetched is None:
                return pr
            merged = pr.model_copy(
                update={
                    "title": fetched.title or pr.title,
                    "body": fetched.body or pr.body,
                    "labels": fetched.labels or pr.labels,
                    "author": fetched.author or pr.author,
                    "is_bot": fetched.is_bot or pr.is_bot,
                    "merged_at": fetched.merged_at or pr.merged_at,
                    "created_at": fetched.created_at or pr.created_at,
                }
            )
            issue_number = self.find_linked_issue_number(merged.body or "")
            if issue_number is not None:
                issue = self.get_issue(issue_number)
                if issue is not None:
                    merged.linked_issue = issue
            return merged
        except Exception:
            return pr
