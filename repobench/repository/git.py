"""Git history ingestion for candidate mining (PRD §65).

Reads merge-commit history from a local clone. Only GitHub-style merge commits
(`Merge pull request #N ...`) are recognized; squash merges are out of scope for V1.
All git access is argv-only via core.gitutil.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from repobench.core.errors import RepoBenchError
from repobench.core.gitutil import git_run, numstat
from repobench.core.types import PRInfo

# GitHub's merge-commit convention: "Merge pull request #123 from owner/branch".
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None


def slug_from_url(url: str) -> str | None:
    """Parse a git remote URL into "owner/repo" (https, ssh and scp-like forms).

    Plain local paths carry no owner/repo semantics and yield None.
    """
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if "://" in url:
        path = urlparse(url).path
    elif ":" in url:
        # scp-like syntax: git@github.com:owner/repo
        path = url.split(":", 1)[1]
    else:
        return None  # local filesystem path, not a hosted slug
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None
    return f"{segments[-2]}/{segments[-1]}"


class GitRepo:
    """Read-only view over a local git repository's history."""

    def __init__(self, path: Path) -> None:
        resolved = Path(path).resolve()
        if not (resolved / ".git").exists():
            raise RepoBenchError(f"not a git repository: {resolved}")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    @property
    def remote_slug(self) -> str | None:
        """\"owner/repo\" for the origin remote, or None when there is no origin."""
        result = git_run(self._root, "remote", "get-url", "origin")
        if result.exit_code != 0:
            return None
        return slug_from_url(result.stdout)

    def merged_prs(self, lookback_days: int, *, now: datetime | None = None) -> list[PRInfo]:
        """Merged PRs (GitHub merge-commit convention) within the lookback window.

        The merge subject carries no PR title/body — GitHub enrichment fills those.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        since_dt = (moment - timedelta(days=lookback_days)).replace(microsecond=0)
        # %z yields "+0000", the offset form git's date parser handles most reliably.
        since = since_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        result = git_run(
            self._root,
            "log",
            "--merges",
            f"--since={since}",
            "--format=%H%x1f%P%x1f%s%x1f%cI",
        )
        if result.exit_code != 0:
            return []

        prs: list[PRInfo] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            merge_sha, parent_field = parts[0], parts[1]
            subject = "\x1f".join(parts[2:-1])  # subject may itself contain the delimiter
            match = _MERGE_PR_RE.match(subject)
            if match is None:
                continue
            parents = parent_field.split()
            if len(parents) < 2:
                continue  # single-parent "merges" are not reconstructable as PRs
            base_sha, head_sha = parents[0], parents[1]
            prs.append(
                PRInfo(
                    number=int(match.group(1)),
                    base_sha=base_sha,
                    head_sha=head_sha,
                    merge_sha=merge_sha,
                    merged_at=_parse_iso(parts[-1]),
                    changed_files=self.changed_files(base_sha, merge_sha),
                )
            )
        return prs

    def changed_files(self, base_sha: str, merge_sha: str) -> list[str]:
        result = git_run(self._root, "diff", "--name-only", f"{base_sha}..{merge_sha}")
        if result.exit_code != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def numstat(self, base_sha: str, merge_sha: str) -> list[tuple[int, int, str]]:
        """Per-file (added, removed, path); binary files report -1/-1."""
        return numstat(self._root, base_sha, merge_sha)

    def pr_title_hint(self, base_sha: str, head_sha: str) -> str:
        """First (up to) 3 commit subjects between base and head — a title fallback."""
        result = git_run(self._root, "log", "--format=%s", "--no-merges", f"{base_sha}..{head_sha}")
        if result.exit_code != 0:
            return ""
        subjects = [line for line in result.stdout.splitlines() if line]
        return "\n".join(subjects[:3])
