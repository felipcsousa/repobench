"""Git history ingestion for candidate mining (PRD §65).

Reads merged-PR history from a local clone. Two GitHub merge styles are
recognized (issue #31): merge commits (`Merge pull request #N ...`, two
parents) and squash merges — a single-parent commit whose subject ends with
`(#N)`; the squashed commit IS the PR, so base is its only parent and head and
merge are the commit itself. Rebase merges leave no PR number in git at all and
stay invisible (surfaced as low recall vs `gh pr list`, never papered over).
All git access is argv-only via core.gitutil.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from repobench.core.errors import RepoBenchError
from repobench.core.gitutil import git_run, numstat
from repobench.core.types import PRInfo

# GitHub's merge-commit convention: "Merge pull request #123 from owner/branch".
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)")

# GitHub's squash convention (issue #31): the PR number is the LAST thing in the
# subject — "feat(payments): support retries (#142)". Digits only, anchored to
# the end of the subject, so ordinary commits like "release: cut (v2)" or
# "chore: bump (#abc)" never match.
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)$")


@dataclass(frozen=True)
class MergeStyleCounts:
    """Window PR counts by GitHub merge style (issue #31).

    Deduped: a PR number seen both as a merge commit and as a squash subject
    counts once, as a merge commit — the merge-commit entry is authoritative.
    """

    merge_commits: int
    squash: int


class _RawPr(NamedTuple):
    """A mined PR before enrichment: SHAs + number + merge time."""

    number: int
    base_sha: str
    head_sha: str
    merge_sha: str
    merged_at: datetime | None


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None


def _window_since(lookback_days: int, now: datetime | None) -> str:
    """`--since` value for the lookback window; one clock shared by all passes."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    since_dt = (moment - timedelta(days=lookback_days)).replace(microsecond=0)
    # %z yields "+0000", the offset form git's date parser handles most reliably.
    return since_dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _recency_key(pr: _RawPr) -> tuple:
    """Newest first; unparseable dates last, number descending as tie-break."""
    timestamp = pr.merged_at.timestamp() if pr.merged_at is not None else 0.0
    return (pr.merged_at is None, -timestamp, -pr.number)


def _dedup_by_number(
    merges: list[_RawPr], squash: list[_RawPr]
) -> list[_RawPr]:
    """Union of both passes, at most one entry per PR number (issue #31).

    A merge-commit entry always wins over a squash entry for the same number —
    its parents are the authoritative PR SHAs. Within a pass, the newest commit
    wins (git log order), so a revert of a squash-merged PR replaces the original.
    """
    by_number: dict[int, _RawPr] = {}
    for raw in [*merges, *squash]:
        by_number.setdefault(raw.number, raw)
    return sorted(by_number.values(), key=_recency_key)


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

    def _scan_commits(
        self, since: str, *filters: str
    ) -> list[tuple[str, list[str], str, datetime | None]]:
        """One `git log` pass: (sha, parents, subject, committed_at) rows."""
        result = git_run(
            self._root,
            "log",
            *filters,
            f"--since={since}",
            "--format=%H%x1f%P%x1f%s%x1f%cI",
        )
        if result.exit_code != 0:
            return []
        rows: list[tuple[str, list[str], str, datetime | None]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            subject = "\x1f".join(parts[2:-1])  # subject may itself contain the delimiter
            rows.append((parts[0], parts[1].split(), subject, _parse_iso(parts[-1])))
        return rows

    def _scan_prs(
        self, lookback_days: int, *, now: datetime | None = None
    ) -> tuple[list[_RawPr], list[_RawPr]]:
        """Merged PRs in the window as (merge-commit PRs, squash PRs).

        Two passes over the same window and clock: `--merges` for GitHub's merge
        convention (base/head = the merge commit's first/second parent) and
        `--no-merges` for the squash convention — a single-parent commit whose
        subject ends in `(#N)` is the PR itself (base = its parent, head = merge
        = the commit). Rebase merges appear in neither pass: git carries no PR
        number for them.
        """
        since = _window_since(lookback_days, now)
        merges: list[_RawPr] = []
        for merge_sha, parents, subject, merged_at in self._scan_commits(since, "--merges"):
            match = _MERGE_PR_RE.match(subject)
            if match is None:
                continue
            if len(parents) < 2:
                continue  # single-parent "merges" are not reconstructable as PRs
            merges.append(
                _RawPr(
                    number=int(match.group(1)),
                    base_sha=parents[0],
                    head_sha=parents[1],
                    merge_sha=merge_sha,
                    merged_at=merged_at,
                )
            )
        squash: list[_RawPr] = []
        for sha, parents, subject, merged_at in self._scan_commits(since, "--no-merges"):
            match = _SQUASH_PR_RE.search(subject.rstrip())
            if match is None:
                continue
            if len(parents) != 1:
                continue  # a squashed PR commit is never a merge (or a root)
            squash.append(
                _RawPr(
                    number=int(match.group(1)),
                    base_sha=parents[0],
                    head_sha=sha,
                    merge_sha=sha,
                    merged_at=merged_at,
                )
            )
        return merges, squash

    def merged_prs(self, lookback_days: int, *, now: datetime | None = None) -> list[PRInfo]:
        """Merged PRs within the lookback window, both GitHub merge styles.

        Deduped by PR number (merge-commit entry wins) and ordered newest first.
        Titles are never mined from git — GitHub enrichment or the local
        subject hint fills them.
        """
        merges, squash = self._scan_prs(lookback_days, now=now)
        return [
            PRInfo(
                number=raw.number,
                base_sha=raw.base_sha,
                head_sha=raw.head_sha,
                merge_sha=raw.merge_sha,
                merged_at=raw.merged_at,
                changed_files=self.changed_files(raw.base_sha, raw.merge_sha),
            )
            for raw in _dedup_by_number(merges, squash)
        ]

    def merge_style_counts(
        self, lookback_days: int, *, now: datetime | None = None
    ) -> MergeStyleCounts:
        """How many window PRs were merge commits vs squash subjects (issue #31).

        Same two passes and dedup rule as `merged_prs`, so the counts always add
        up to what mining sees; needed for the analyze merge-style warning.
        """
        merges, squash = self._scan_prs(lookback_days, now=now)
        merge_numbers = {raw.number for raw in merges}
        squash_only = sum(1 for raw in squash if raw.number not in merge_numbers)
        return MergeStyleCounts(merge_commits=len(merge_numbers), squash=squash_only)

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
