"""Repository-layer tests: GitRepo history mining, remote slugs, workload statistics
and the gh-backed GitHubClient (monkeypatched run_sync — never online)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.gitutil import commit_all, git, make_pr, squash_pr
from repobench.config import RepoBenchConfig, TaskMiningConfig
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
from repobench.cli.render import render_analyze_summary, render_merge_style_warnings
from repobench.cli.services import AnalyzeOutcome, analyze_repository
from repobench.mining.candidates import mine_candidates
from repobench.repository.git import GitRepo, MergeStyleCounts, slug_from_url
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
    squash_pr(git_repo, 2, {"b.py": "x = 2\n"}, "feat: add b")
    gr = GitRepo(git_repo)
    now = datetime.now(timezone.utc)
    # since = tomorrow -> nothing can be inside the window (both merge styles)
    assert gr.merged_prs(lookback_days=0, now=now + timedelta(days=1)) == []
    assert len(gr.merged_prs(lookback_days=180, now=now)) == 2


def test_merged_prs_empty_history(git_repo: Path) -> None:
    assert GitRepo(git_repo).merged_prs(180) == []


def test_merged_prs_finds_squash_pr(git_repo: Path) -> None:
    """Issue #31: a squash merge has no merge commit — the squashed commit IS
    the PR (base = its single parent, head = merge = itself)."""
    sha = squash_pr(git_repo, 142, {"src/pay.py": "charge()\n"}, "feat(payments): support retries")
    prs = GitRepo(git_repo).merged_prs(lookback_days=180, now=datetime.now(timezone.utc))
    assert [pr.number for pr in prs] == [142]
    pr = prs[0]
    assert pr.base_sha == git(git_repo, "rev-parse", f"{sha}^")
    assert pr.head_sha == pr.merge_sha == sha
    assert pr.merged_at is not None and pr.merged_at.tzinfo is not None
    assert pr.changed_files == ["src/pay.py"]
    # title is never mined from git; enrichment (gh or the subject hint) fills it
    assert pr.title == ""


def test_merged_prs_mixed_styles_unique_and_ordered(git_repo: Path) -> None:
    make_pr(git_repo, 1, "feat/a", {"a.py": "x = 1\n"}, "add a")
    squash_pr(git_repo, 2, {"b.py": "x = 2\n"}, "feat: add b")
    make_pr(git_repo, 3, "feat/c", {"c.py": "x = 3\n"}, "add c")
    prs = GitRepo(git_repo).merged_prs(180, now=datetime.now(timezone.utc))
    numbers = [pr.number for pr in prs]
    assert numbers == [3, 2, 1]  # newest first across styles; no duplicate numbers
    assert len(set(numbers)) == len(numbers)
    by_number = {pr.number: pr for pr in prs}
    assert by_number[2].head_sha == by_number[2].merge_sha  # squash entry
    for merge_number in (1, 3):
        pr = by_number[merge_number]
        assert len({pr.base_sha, pr.head_sha, pr.merge_sha}) == 3


def test_merged_prs_merge_commit_wins_dedup(git_repo: Path) -> None:
    """Synthetic collision: a merge commit whose subject ALSO carries (#5), plus a
    later squash commit claiming the same number — the merge-commit entry wins."""
    git(git_repo, "checkout", "--quiet", "-b", "feat/dup")
    (git_repo / "d.py").write_text("x = 4\n")
    commit_all(git_repo, "add d")
    git(git_repo, "checkout", "--quiet", "main")
    git(
        git_repo,
        "merge",
        "--quiet",
        "--no-gpg-sign",
        "--no-ff",
        "-m",
        "Merge pull request #5 from acme/feat-dup (#5)",
        "feat/dup",
    )
    merge_sha = git(git_repo, "rev-parse", "HEAD")
    squash_pr(git_repo, 5, {"e.py": "x = 5\n"}, "feat: duplicate five")
    prs = GitRepo(git_repo).merged_prs(180, now=datetime.now(timezone.utc))
    assert [pr.number for pr in prs] == [5]
    assert prs[0].merge_sha == merge_sha
    parents = git(git_repo, "show", "-s", "--format=%P", merge_sha).split()
    assert prs[0].base_sha == parents[0] and prs[0].head_sha == parents[1]


def test_squash_subject_requires_trailing_number(git_repo: Path) -> None:
    """Ordinary commits ending in a parenthetical are not PRs: the regex needs
    digits inside `(#N)` anchored at the very end of the subject."""
    subjects = (
        "release: cut (v2)",
        "chore: bump (#abc)",
        "fix: retry (#12extra)",
        "feat: mention (#12) mid-subject",
        "root commit without parents marker (#)",
    )
    for subject in subjects:
        git(git_repo, "commit", "--quiet", "--no-gpg-sign", "--allow-empty", "-m", subject)
    assert GitRepo(git_repo).merged_prs(180, now=datetime.now(timezone.utc)) == []


def test_merge_style_counts_both_styles(git_repo: Path) -> None:
    make_pr(git_repo, 1, "feat/a", {"a.py": "x = 1\n"}, "add a")
    squash_pr(git_repo, 2, {"b.py": "x = 2\n"}, "feat: add b")
    squash_pr(git_repo, 3, {"c.py": "x = 3\n"}, "feat: add c")
    gr = GitRepo(git_repo)
    now = datetime.now(timezone.utc)
    assert gr.merge_style_counts(180, now=now) == MergeStyleCounts(merge_commits=1, squash=2)
    # same window semantics as merged_prs: an empty window counts nothing
    assert gr.merge_style_counts(0, now=now + timedelta(days=1)) == MergeStyleCounts(0, 0)


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


def test_merged_pr_count_filters_by_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ground truth for recall (issue #31): only PRs merged inside the window count."""
    now = datetime.now(timezone.utc)
    payload = [
        {"number": 1, "mergedAt": now.isoformat()},
        {"number": 2, "mergedAt": (now - timedelta(days=3)).isoformat()},
        {"number": 3, "mergedAt": (now - timedelta(days=400)).isoformat()},  # outside window
        {"number": 4, "mergedAt": None},  # unplaceable — skipped
    ]
    seen: list[list[str]] = []

    def fake(argv: list[str], cwd: Path, **kwargs: object) -> ProcessResult:
        seen.append(argv)
        assert argv[0] == "gh" and kwargs.get("timeout_seconds") == 30
        return ProcessResult(exit_code=0, stdout=json.dumps(payload))

    monkeypatch.setattr("repobench.repository.github.run_sync", fake)
    since = (now - timedelta(days=180)).replace(microsecond=0)
    assert GitHubClient("acme/payments").merged_pr_count(since) == 2
    argv = seen[0]
    assert argv[:4] == ["gh", "pr", "list", "--state"]
    assert "merged" in argv and "--limit" in argv and "500" in argv
    assert "number,mergedAt" in argv


def test_merged_pr_count_failures_return_none(monkeypatch: pytest.MonkeyPatch) -> None:
    since = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "repobench.repository.github.run_sync",
        lambda argv, cwd, **kw: ProcessResult(exit_code=None, stderr="offline"),
    )
    assert GitHubClient("acme/payments").merged_pr_count(since) is None
    monkeypatch.setattr(
        "repobench.repository.github.run_sync",
        lambda argv, cwd, **kw: ProcessResult(exit_code=0, stdout="not json"),
    )
    assert GitHubClient("acme/payments").merged_pr_count(since) is None


def _patch_gh_list(monkeypatch: pytest.MonkeyPatch, merged: list[dict]) -> None:
    """gh fake for the analyze path: `pr list` answers with `merged`; every other
    gh call fails, so enrichment/visibility degrade to None as in the field."""

    def fake(argv: list[str], cwd: Path, **kwargs: object) -> ProcessResult:
        if "list" in argv:
            return ProcessResult(exit_code=0, stdout=json.dumps(merged))
        return ProcessResult(exit_code=1, stderr="nope")

    monkeypatch.setattr("repobench.repository.github.run_sync", fake)


def test_analyze_repository_recall_against_gh_ground_truth(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mined 2 squash PRs, gh says 3 merged in the window -> recall 2/3 (issue #31)."""
    squash_pr(git_repo, 1, {"a.py": "x = 1\n"}, "feat: add a")
    squash_pr(git_repo, 2, {"b.py": "x = 2\n"}, "feat: add b")
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/payments.git")
    now = datetime.now(timezone.utc)
    _patch_gh_list(
        monkeypatch,
        [
            {"number": 1, "mergedAt": now.isoformat()},
            {"number": 2, "mergedAt": now.isoformat()},
            {"number": 3, "mergedAt": now.isoformat()},  # e.g. rebase-merged: invisible
        ],
    )
    monkeypatch.setattr(
        "repobench.cli.services.shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None
    )
    outcome = analyze_repository(git_repo, RepoBenchConfig())
    assert outcome.enrichment == "github"
    assert outcome.merged_prs == 2
    assert outcome.merge_styles == MergeStyleCounts(merge_commits=0, squash=2)
    assert outcome.recall_total == 3


def test_analyze_repository_without_gh_claims_no_recall(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    squash_pr(git_repo, 4, {"a.py": "x = 1\n"}, "feat: add a")
    monkeypatch.setattr("repobench.cli.services.shutil.which", lambda name: None)
    outcome = analyze_repository(git_repo, RepoBenchConfig())
    assert outcome.enrichment == "local"
    assert outcome.merged_prs == 1
    assert outcome.merge_styles == MergeStyleCounts(merge_commits=0, squash=1)
    assert outcome.recall_total is None  # no gh ground truth — nothing invented


def _analyze_outcome(**overrides: object) -> AnalyzeOutcome:
    data: dict[str, object] = dict(
        summary=summarize_analysis(25, [], 5),
        candidates=[],
        merged_prs=25,
        enrichment="github",
        remote_slug="acme/payments",
        merge_styles=MergeStyleCounts(merge_commits=12, squash=13),
        recall_total=58,
    )
    data.update(overrides)
    return AnalyzeOutcome(**data)


def test_render_analyze_shows_merge_style_and_recall(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = _analyze_outcome()
    render_analyze_summary(outcome, 5, "note")
    render_merge_style_warnings(outcome)
    out = capsys.readouterr().out
    assert "Merge style" in out and "12 merge commits · 13 squash" in out
    assert "Recall vs GitHub" in out and "25/58 merged PRs (43%)" in out
    assert (
        "⚠ merge style: 13 squash-merged PRs mined from commit subjects (#N) — "
        "no merge commits for them exist"
    ) in out
    assert (
        "⚠ low recall: 33 of 58 merged PRs in the window are invisible to mining "
        "— PRs merged by rebase carry no PR number in git at all"
    ) in out


def test_render_analyze_without_gh_claims_no_recall(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = _analyze_outcome(
        enrichment="local",
        remote_slug=None,
        merge_styles=MergeStyleCounts(merge_commits=2, squash=0),
        recall_total=None,
    )
    render_analyze_summary(outcome, 0, "note")
    render_merge_style_warnings(outcome)
    out = capsys.readouterr().out
    assert "2 merge commits" in out
    assert "Recall vs GitHub" not in out
    assert "recall" not in out.lower()  # gh unavailable — recall is never invented
    assert "⚠" not in out  # no squash PRs, no ground truth: no warnings


def test_mining_squash_pr_respects_bot_filter(git_repo: Path) -> None:
    squash_pr(git_repo, 9, {"src/gw.py": "a\nb\nc\nd\ne\n"}, "bump gateway")

    def bot_enrich(pr: PRInfo) -> PRInfo:
        return pr.model_copy(update={"author": "dependabot[bot]", "is_bot": True})

    candidates = mine_candidates(
        GitRepo(git_repo), TaskMiningConfig(), enrich=bot_enrich
    )
    assert candidates == []  # bot squash PRs never become candidates (PRD §70)


def test_find_linked_issue_number_regex() -> None:
    find = GitHubClient.find_linked_issue_number
    assert find("blah\nFixes #12") == 12
    assert find("closes #3 for real") == 3
    assert find("Resolves: #9") == 9
    assert find("fix #5 please") == 5
    assert find("no references here") is None
    assert find("") is None
