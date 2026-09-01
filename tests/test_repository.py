"""Repository-layer tests: GitRepo history mining, remote slugs, workload statistics
and the gh-backed GitHubClient (monkeypatched run_sync — never online)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.gitutil import git, make_pr
from repobench.core.errors import RepoBenchError
from repobench.core.types import (
    Assessment,
    CandidateInfo,
    Complexity,
    PRInfo,
    ProcessResult,
    TaskStatus,
    TaskType,
)
from repobench.repository.git import GitRepo, slug_from_url
from repobench.repository.github import GitHubClient
from repobench.repository.workload import (
    build_workload,
    distribution,
    suggest_benchmark_size,
    summarize_analysis,
)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "history"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _candidate(
    task_type: TaskType = TaskType.UNKNOWN,
    subsystem: str = "unknown",
    complexity: Complexity = Complexity.MEDIUM,
) -> CandidateInfo:
    return CandidateInfo(
        candidate_id="c_x",
        pr=PRInfo(number=1),
        assessment=Assessment(task_type=task_type, subsystem=subsystem, complexity=complexity),
    )


def test_git_repo_requires_git_dir(tmp_path: Path) -> None:
    with pytest.raises(RepoBenchError):
        GitRepo(tmp_path)


def test_merged_prs_finds_merge_commit(git_repo: Path) -> None:
    make_pr(
        git_repo,
        7,
        "feat/x",
        {"src/core.py": "value = 1\n", "tests/test_core.py": "def test_it():\n    assert True\n"},
        "add core module",
    )
    gr = GitRepo(git_repo)
    prs = gr.merged_prs(lookback_days=180, now=datetime.now(timezone.utc))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 7
    assert pr.title == ""  # merge subjects carry no title; enrichment fills it
    assert pr.base_sha and pr.head_sha and pr.merge_sha
    assert len({pr.base_sha, pr.head_sha, pr.merge_sha}) == 3
    # first parent = base, second parent = feature head (checked against raw git
    # plumbing, independent of GitRepo's own parsing)
    parents = git(git_repo, "show", "-s", "--format=%P", pr.merge_sha).split()
    assert parents[0] == pr.base_sha and parents[1] == pr.head_sha
    assert pr.merged_at is not None and pr.merged_at.tzinfo is not None
    assert set(pr.changed_files) == {"src/core.py", "tests/test_core.py"}


def test_merged_prs_respects_lookback_window(git_repo: Path) -> None:
    make_pr(git_repo, 1, "feat/a", {"a.py": "x = 1\n"}, "add a")
    gr = GitRepo(git_repo)
    # since = tomorrow -> nothing can be inside the window
    assert gr.merged_prs(lookback_days=0, now=datetime.now(timezone.utc) + timedelta(days=1)) == []
    assert len(gr.merged_prs(lookback_days=180, now=datetime.now(timezone.utc))) == 1


def test_merged_prs_empty_history(git_repo: Path) -> None:
    assert GitRepo(git_repo).merged_prs(180) == []


def test_changed_files_numstat_and_title_hint(git_repo: Path) -> None:
    make_pr(
        git_repo,
        3,
        "feat/stats",
        {
            "src/util.py": "one\ntwo\nthree\n",
            "tests/test_util.py": "assert True\n",
        },
        "add util module",
    )
    gr = GitRepo(git_repo)
    pr = gr.merged_prs(180)[0]

    assert gr.changed_files(pr.base_sha, pr.merge_sha) == ["src/util.py", "tests/test_util.py"]
    assert gr.changed_files("0" * 40, pr.merge_sha) == []

    stats = {path: (added, removed) for added, removed, path in gr.numstat(pr.base_sha, pr.merge_sha)}
    assert stats["src/util.py"] == (3, 0)
    assert stats["tests/test_util.py"] == (1, 0)

    hint = gr.pr_title_hint(pr.base_sha, pr.head_sha)
    assert hint == "add util module"


def test_remote_slug_with_and_without_origin(git_repo: Path) -> None:
    gr = GitRepo(git_repo)
    assert gr.remote_slug is None
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/payments.git")
    assert gr.remote_slug == "acme/payments"
    git(git_repo, "remote", "set-url", "origin", "git@github.com:acme/widgets.git")
    assert gr.remote_slug == "acme/widgets"


def test_slug_from_url_forms() -> None:
    assert slug_from_url("https://github.com/acme/payments.git") == "acme/payments"
    assert slug_from_url("https://github.com/acme/payments") == "acme/payments"
    assert slug_from_url("git@github.com:acme/payments.git") == "acme/payments"
    assert slug_from_url("ssh://git@github.com/acme/payments.git") == "acme/payments"
    assert slug_from_url("/local/path/only") is None


def test_distribution_shares() -> None:
    assert distribution([]) == {}
    shares = distribution(["bugfix", "bugfix", "feature"])
    assert shares["bugfix"] == pytest.approx(2 / 3)
    assert shares["feature"] == pytest.approx(1 / 3)
    assert sum(shares.values()) == pytest.approx(1.0)


def test_build_workload_dimensions() -> None:
    candidates = [
        _candidate(TaskType.BUGFIX, "src", Complexity.SMALL),
        _candidate(TaskType.FEATURE, "src", Complexity.LARGE),
    ]
    workload = build_workload(candidates)
    assert workload.task_type == {"bugfix": 0.5, "feature": 0.5}
    assert workload.subsystem == {"src": 1.0}
    assert workload.complexity == {"small": 0.5, "large": 0.5}


def test_suggest_benchmark_size_clamps_15_to_30() -> None:
    assert suggest_benchmark_size(0) == 0
    assert suggest_benchmark_size(7) == 7
    assert suggest_benchmark_size(15) == 15
    assert suggest_benchmark_size(24) == 24
    assert suggest_benchmark_size(45) == 30


def test_summarize_analysis_counts_validated_only() -> None:
    good = _candidate(TaskType.BUGFIX)
    filtered = _candidate(TaskType.UNKNOWN).model_copy(
        update={"status": TaskStatus.FILTERED}
    )
    summary = summarize_analysis(
        total_merged_prs=10, candidates=[good, filtered], suggested_size=15
    )
    assert summary.total_merged_prs == 10
    assert summary.task_candidates == 2
    assert summary.validated_candidates == 1
    assert summary.suggested_benchmark_size == 15
    assert summary.workload.task_type == {"bugfix": 0.5, "unknown": 0.5}


def _patch_gh(monkeypatch: pytest.MonkeyPatch, payload_by_kind: dict[str, object]) -> None:
    def fake_run_sync(argv: list[str], cwd: Path, **kwargs: object) -> ProcessResult:
        assert argv[0] == "gh"
        assert kwargs.get("timeout_seconds") == 30
        for kind, payload in payload_by_kind.items():
            if kind in argv:
                return ProcessResult(exit_code=0, stdout=json.dumps(payload))
        return ProcessResult(exit_code=1, stderr="nope")

    monkeypatch.setattr("repobench.repository.github.run_sync", fake_run_sync)


def test_github_client_parses_pr_json(monkeypatch: pytest.MonkeyPatch) -> None:
    pr_json = {
        "number": 7,
        "title": "Fix charge rounding",
        "body": "Fixes #42\n\nThe charged amount is off by a cent for mixed carts.",
        "labels": [{"name": "bug"}, {"name": "payments"}],
        "author": {"login": "alice", "__typename": "User"},
        "mergedAt": "2026-08-01T12:00:00Z",
        "createdAt": "2026-07-30T09:00:00Z",
        "baseRefOid": "a" * 40,
        "headRefOid": "b" * 40,
    }
    _patch_gh(monkeypatch, {"pr": pr_json})
    client = GitHubClient("acme/payments")
    pr = client.get_pr(7)
    assert pr is not None
    assert pr.number == 7
    assert pr.title == "Fix charge rounding"
    assert pr.labels == ["bug", "payments"]
    assert pr.author == "alice" and not pr.is_bot
    # SHAs are git-authoritative (merge-commit parents), never taken from gh.
    assert pr.base_sha is None and pr.head_sha is None
    assert pr.merged_at == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert pr.created_at == datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)


def test_github_client_flags_bot_authors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(
        monkeypatch,
        {
            "pr": {
                "number": 9,
                "title": "Bump dep",
                "body": "",
                "labels": [],
                "author": {"login": "dependabot[bot]", "__typename": "Bot"},
                "mergedAt": None,
                "createdAt": None,
                "baseRefOid": None,
                "headRefOid": None,
            }
        },
    )
    pr = GitHubClient("acme/payments").get_pr(9)
    assert pr is not None and pr.is_bot and pr.author == "dependabot[bot]"


def test_github_client_enrich_fills_linked_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(
        monkeypatch,
        {
            "pr": {
                "number": 7,
                "title": "Fix charge rounding",
                "body": "This resolves #42 for mixed carts.",
                "labels": [],
                "author": {"login": "alice", "__typename": "User"},
                "mergedAt": None,
                "createdAt": None,
                "baseRefOid": None,
                "headRefOid": None,
            },
            "issue": {
                "number": 42,
                "title": "Rounding wrong",
                "body": "Totals are off by a cent.",
                "createdAt": "2026-07-01T00:00:00Z",
            },
        },
    )
    client = GitHubClient("acme/payments")
    enriched = client.enrich(PRInfo(number=7))
    assert enriched.title == "Fix charge rounding"
    assert enriched.linked_issue is not None
    assert enriched.linked_issue.number == 42
    assert enriched.linked_issue.title == "Rounding wrong"


def test_github_enrich_preserves_git_authoritative_shas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: gh's baseRefOid tracks the live branch tip and must never replace
    the merge-commit parent SHAs the mining layer derives ids/diffs/archives from."""
    _patch_gh(
        monkeypatch,
        {
            "pr": {
                "number": 7,
                "title": "Fix charge rounding",
                "body": "",
                "labels": [],
                "author": {"login": "alice", "__typename": "User"},
                "mergedAt": None,
                "createdAt": None,
            }
        },
    )
    git_base, git_head, git_merge = "1" * 40, "2" * 40, "3" * 40
    pr = PRInfo(
        number=7, base_sha=git_base, head_sha=git_head, merge_sha=git_merge
    )
    enriched = GitHubClient("acme/payments").enrich(pr)
    assert enriched.base_sha == git_base
    assert enriched.head_sha == git_head
    assert enriched.merge_sha == git_merge


def test_github_client_failures_return_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(argv: list[str], cwd: Path, **kwargs: object) -> ProcessResult:
        return ProcessResult(exit_code=None, stderr="spawn failed: gh missing")

    monkeypatch.setattr("repobench.repository.github.run_sync", failing)
    client = GitHubClient("acme/payments")
    assert client.get_pr(1) is None
    assert client.get_issue(1) is None
    base = PRInfo(number=1, title="local")
    assert client.enrich(base) == base  # never raises, returns the input untouched


def test_github_client_bad_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "repobench.repository.github.run_sync",
        lambda argv, cwd, **kw: ProcessResult(exit_code=0, stdout="not json"),
    )
    assert GitHubClient("acme/payments").get_pr(1) is None
    assert GitHubClient("acme/payments").get_issue(1) is None


def test_find_linked_issue_number_regex() -> None:
    find = GitHubClient.find_linked_issue_number
    assert find("blah\nFixes #12") == 12
    assert find("closes #3 for real") == 3
    assert find("Resolves: #9") == 9
    assert find("fix #5 please") == 5
    assert find("no references here") is None
    assert find("") is None
