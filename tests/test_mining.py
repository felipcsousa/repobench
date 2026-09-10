"""Mining tests: task-type classification, complexity buckets, subsystem inference,
instruction provenance, and end-to-end candidate mining over temporary git repos."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.gitutil import git, make_pr, squash_pr
from repobench.config import ComplexityConfig, ProjectConfig, TaskMiningConfig
from repobench.core.types import (
    CandidateClass,
    Complexity,
    IssueInfo,
    PRInfo,
    RejectionCode,
    TaskStatus,
    TaskType,
)
from repobench.mining.candidates import mine_candidates
from repobench.mining.classification import classify_task_type
from repobench.mining.complexity import compute_complexity
from repobench.mining.instruction import derive_instruction
from repobench.mining.subsystem import infer_subsystem
from repobench.repository.git import GitRepo

# Relaxed sizes: good PR has 6 impl LOC, tiny 2, oversized 30.
CFG = TaskMiningConfig(min_implementation_loc=5, max_implementation_loc=20, max_implementation_files=8)

CHARGE_IMPL = (
    "def charge(total):\n"
    "    cents = round(total * 100)\n"
    "    if cents <= 0:\n"
    "        raise ValueError('empty cart')\n"
    "    taxed = cents * 1.2\n"
    "    return round(taxed)\n"
)
REFUND_IMPL = "def refund(cents):\n    if cents <= 0:\n        raise ValueError\n    return -cents\n    # pad\n"
TIP_IMPL = "def tip(total):\n    return total\n"
BULK_IMPL = "line = 1\n" * 30

# Enrichment payloads: number -> (title, body). Bodies >= 80 chars give confidence B.
ENRICH_TEXTS = {
    1: (
        "fix: charge rounding drops cents",
        "Customers report the charged amount is off by a few cents whenever the cart mixes discounted items with taxes applied.",
    ),
    2: (
        "add refund endpoint",
        "Adds a refund endpoint so support staff can return funds without editing the ledger by hand during an incident.",
    ),
    3: ("add tip helper", "Small helper."),
    4: (
        "add bulk upload pipeline",
        "Adds a bulk upload pipeline so enterprise merchants can import large catalogs without scripted API calls.",
    ),
    5: (
        "fix: charge rounding drops cents",
        "Customers report the charged amount is off by a few cents whenever the cart mixes discounted items with taxes applied.",
    ),
}


@pytest.fixture()
def mining_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "history"
    repo.mkdir()
    (repo / "README.md").write_text("demo\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")

    make_pr(
        repo,
        1,
        "fix/charge",
        {
            "src/payments/charge.py": CHARGE_IMPL,
            "tests/test_charge.py": "def test_charge():\n    assert charge(1.0) == 120\n",
        },
        "fix charge rounding",
    )
    # impl-only PR: implementation size is fine, but no test change
    make_pr(repo, 2, "feat/refund", {"src/payments/refund.py": REFUND_IMPL}, "add refund")
    # tiny PR: implementation below min_implementation_loc
    make_pr(
        repo,
        3,
        "feat/tip",
        {
            "src/payments/tip.py": TIP_IMPL,
            "tests/test_tip.py": "def test_tip():\n    assert True\n",
        },
        "add tip",
    )
    # oversized PR: implementation above max_implementation_loc
    make_pr(
        repo,
        4,
        "feat/bulk",
        {
            "src/payments/bulk.py": BULK_IMPL,
            "tests/test_bulk.py": "def test_bulk():\n    assert True\n",
        },
        "add bulk",
    )
    return repo


def _text_enricher(pr: PRInfo) -> PRInfo:
    title, body = ENRICH_TEXTS[pr.number]
    return pr.model_copy(update={"title": title, "body": body})


# ---------------------------------------------------------------- classification


def test_label_wins_over_title_and_convention() -> None:
    pr = PRInfo(number=1, labels=["bug"], title="feat: add checkout")
    assert classify_task_type(pr, ["src/x.py"]) is TaskType.BUGFIX


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("bug", TaskType.BUGFIX),
        ("bugfix", TaskType.BUGFIX),
        ("feature", TaskType.FEATURE),
        ("refactor", TaskType.REFACTOR),
        ("performance", TaskType.PERFORMANCE),
        ("dependencies", TaskType.INTEGRATION),
        ("migration", TaskType.MIGRATION),
        ("ci", TaskType.INFRASTRUCTURE),
        ("docker", TaskType.INFRASTRUCTURE),
    ],
)
def test_label_categories(label: str, expected: TaskType) -> None:
    assert classify_task_type(PRInfo(number=1, labels=[label]), []) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat: add checkout", TaskType.FEATURE),
        ("fix(core): stop crash on empty cart", TaskType.BUGFIX),
        ("refactor: split charge module", TaskType.REFACTOR),
        ("perf: speed up indexing", TaskType.PERFORMANCE),
        ("chore(deps): bump pydantic to 2.9", TaskType.INTEGRATION),
        ("build: publish wheels", TaskType.INFRASTRUCTURE),
        ("ci: run tests on windows", TaskType.INFRASTRUCTURE),
    ],
)
def test_conventional_commit_prefix(title: str, expected: TaskType) -> None:
    assert classify_task_type(PRInfo(number=1, title=title), ["src/x.py"]) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Fix crash when cart is empty", TaskType.BUGFIX),
        ("Add support for TLS 1.3", TaskType.FEATURE),
        ("Migrate billing to pydantic v2", TaskType.MIGRATION),
        ("Speed up the indexer", TaskType.PERFORMANCE),
        ("Refactor auth module", TaskType.REFACTOR),
        ("Bump request dependency to 2.32", TaskType.INTEGRATION),
        ("Totally unrelated change", TaskType.UNKNOWN),
    ],
)
def test_title_patterns(title: str, expected: TaskType) -> None:
    assert classify_task_type(PRInfo(number=1, title=title), []) is expected


def test_diff_fallback_test_only_and_infra_only() -> None:
    pr = PRInfo(number=1)
    assert classify_task_type(pr, ["tests/test_a.py", "tests/test_b.py"]) is TaskType.UNKNOWN
    assert (
        classify_task_type(pr, [".github/workflows/ci.yml", "Dockerfile"])
        is TaskType.INFRASTRUCTURE
    )
    assert classify_task_type(pr, []) is TaskType.UNKNOWN


# ---------------------------------------------------------------- complexity


def test_complexity_buckets() -> None:
    cfg = TaskMiningConfig()  # small_loc_max=50, large_loc_min=200, large_files_min=5
    assert compute_complexity(10, 1, 1, cfg) is Complexity.SMALL
    assert compute_complexity(50, 2, 1, cfg) is Complexity.SMALL
    assert compute_complexity(60, 2, 1, cfg) is Complexity.MEDIUM
    assert compute_complexity(100, 4, 2, cfg) is Complexity.MEDIUM
    assert compute_complexity(200, 1, 1, cfg) is Complexity.LARGE
    assert compute_complexity(10, 5, 1, cfg) is Complexity.LARGE
    assert compute_complexity(10, 1, 3, cfg) is Complexity.LARGE


# ---------------------------------------------------------------- subsystem


def test_subsystem_codeowners_longest_prefix() -> None:
    owners = {"src": "core-team", "src/payments": "payments-team"}
    assert infer_subsystem(["src/payments/charge.py"], codeowners=owners) == "payments-team"
    assert infer_subsystem(["src/util.py"], codeowners={"src/": "core-team"}) == "core-team"
    assert infer_subsystem(["docs/x.md"], codeowners={"*": "everyone"}) == "everyone"


def test_subsystem_package_dirs() -> None:
    assert (
        infer_subsystem(["packages/core/src/engine.py"], package_dirs={"packages/core": "core"})
        == "core"
    )
    # first changed file that matches a package dir wins
    assert (
        infer_subsystem(
            ["docs/readme.md", "packages/core/a.py"], package_dirs={"packages/core": "core"}
        )
        == "core"
    )


def test_subsystem_stable_segment_and_fallbacks() -> None:
    assert infer_subsystem(["src/payments/a.py", "tests/test_a.py"]) == "src"
    assert infer_subsystem(["README.md"]) == "root"
    assert infer_subsystem([]) == "unknown"


# ---------------------------------------------------------------- instruction


PR_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)
ISSUE_AT = PR_AT - timedelta(days=3)  # the issue predates the PR (PRD §71)


def test_instruction_from_linked_issue_is_confidence_a() -> None:
    pr = PRInfo(
        number=1,
        title="Fix charge",
        body="x" * 100,
        created_at=PR_AT,
        linked_issue=IssueInfo(
            number=5,
            title="Cart totals wrong",
            body="Reproduce by mixing items.",
            created_at=ISSUE_AT,
        ),
    )
    result = derive_instruction(pr)
    assert result is not None
    assert result.confidence == "A"
    assert result.source == "issue"
    assert result.text.startswith("Cart totals wrong")


def test_instruction_issue_created_with_pr_does_not_get_tier_a() -> None:
    # Same-day issue (created after the PR) is NOT pre-solution intent (PRD §71)
    # — the instruction falls through to the PR body/title tiers.
    pr = PRInfo(
        number=1,
        title="Fix charge",
        created_at=PR_AT,
        linked_issue=IssueInfo(
            number=5, title="Cart totals wrong", created_at=PR_AT + timedelta(hours=1)
        ),
    )
    result = derive_instruction(pr)
    assert result is not None and result.confidence == "C"  # title fallback
    assert result.source == "title"


def test_instruction_issue_without_timestamps_does_not_get_tier_a() -> None:
    # Unverifiable provenance never claims tier A (PRD §71).
    pr = PRInfo(
        number=1,
        title="Fix charge",
        linked_issue=IssueInfo(number=5, title="Cart totals wrong"),
    )
    result = derive_instruction(pr)
    assert result is not None and result.confidence == "C"


def test_instruction_long_clean_body_is_confidence_b() -> None:
    body = (
        "Customers report the charged amount drifts by one cent whenever discounts "
        "and taxes combine in a single cart at checkout."
    )
    result = derive_instruction(PRInfo(number=1, title="t", body=body))
    assert result is not None
    assert result.confidence == "B"
    assert result.source == "pr_body"
    assert result.text == body


def test_instruction_code_fence_is_contaminated() -> None:
    body = "The checkout endpoint returns 500.\n```\nstack trace line\n```\nRepro every time on main."
    result = derive_instruction(PRInfo(number=1, body=body))
    assert result is not None
    assert result.confidence == "C" and result.source == "pr_body"


def test_instruction_fix_phrases_are_contaminated() -> None:
    body = (
        "This was fixed by switching the rounding mode in the charge service; "
        "the change is small and safe to ship this week."
    )
    result = derive_instruction(PRInfo(number=1, body=body))
    assert result is not None and result.confidence == "C"


def test_instruction_title_only_is_confidence_c() -> None:
    result = derive_instruction(PRInfo(number=1, title="Fix charge rounding"))
    assert result is not None
    assert result.confidence == "C"
    assert result.source == "title"
    assert result.text == "Fix charge rounding"


def test_instruction_absent() -> None:
    assert derive_instruction(PRInfo(number=1)) is None


def test_instruction_issue_body_trimmed_to_4000() -> None:
    result = derive_instruction(
        PRInfo(
            number=1,
            created_at=PR_AT,
            linked_issue=IssueInfo(
                number=2, title="t", body="x" * 5000, created_at=ISSUE_AT
            ),
        )
    )
    assert result is not None
    assert len(result.text) == len("t\n\n") + 4000


# ---------------------------------------------------------------- mine_candidates


def test_mine_candidates_end_to_end(mining_repo: Path) -> None:
    candidates = mine_candidates(GitRepo(mining_repo), CFG, enrich=_text_enricher)
    by_number = {candidate.pr.number: candidate for candidate in candidates}
    assert set(by_number) == {1, 2, 3, 4}

    good = by_number[1]
    assert good.status is TaskStatus.DISCOVERED and good.rejection_code is None
    assert good.candidate_id.startswith("c_1_")
    assert good.assessment.task_type is TaskType.BUGFIX  # conventional-commit prefix on enriched title
    assert good.assessment.complexity is Complexity.SMALL  # 6 impl LOC, 1 file, 1 package
    assert good.assessment.subsystem == "src"
    assert good.assessment.implementation_loc == 6 and good.assessment.implementation_files == 1
    assert good.assessment.test_loc == 2 and good.assessment.test_files == 1
    assert good.assessment.instruction_confidence == "B" and good.assessment.instruction_source == "pr_body"
    assert good.assessment.instruction.startswith("Customers report")
    assert good.pr.merged_at is not None  # created_at == merged_at lives on the embedded PRInfo

    assert by_number[2].status is TaskStatus.FILTERED
    assert by_number[2].rejection_code is RejectionCode.NO_TEST_CHANGE
    assert by_number[3].rejection_code is RejectionCode.TASK_TOO_SMALL
    assert by_number[4].rejection_code is RejectionCode.TASK_TOO_LARGE


def test_mine_candidates_includes_squash_pr(mining_repo: Path) -> None:
    """Issue #31: a squash-merged PR (subject `(#N)`, single parent, no merge
    commit) is minable exactly like a merge-commit PR."""
    squash_pr(
        mining_repo,
        5,
        {
            "src/payments/gateway.py": "a\nb\nc\nd\ne\n",
            "tests/test_gateway.py": "x\ny\n",
        },
        "fix: charge rounding drops cents",
    )
    candidates = mine_candidates(GitRepo(mining_repo), CFG, enrich=_text_enricher)
    by_number = {candidate.pr.number: candidate for candidate in candidates}
    assert set(by_number) == {1, 2, 3, 4, 5}
    squashed = by_number[5]
    assert squashed.status is TaskStatus.DISCOVERED and squashed.rejection_code is None
    # the squashed commit IS the PR: head and merge are the same commit
    assert squashed.pr.head_sha == squashed.pr.merge_sha
    assert squashed.pr.base_sha != squashed.pr.merge_sha


def test_mine_candidates_skips_bots_and_bad_enrichment(mining_repo: Path) -> None:
    make_pr(
        mining_repo,
        5,
        "chore/dep",
        {
            "src/payments/gateway.py": "a\nb\nc\nd\ne\n",
            "tests/test_gateway.py": "x\ny\n",
        },
        "bump gateway",
    )

    def enricher(pr: PRInfo) -> PRInfo | None:
        if pr.number == 5:
            return pr.model_copy(
                update={"title": "bump gateway dep", "body": "y" * 100, "is_bot": True}
            )
        if pr.number == 3:
            return None  # unusable enrichment result must be ignored
        return _text_enricher(pr)

    candidates = mine_candidates(GitRepo(mining_repo), CFG, enrich=enricher)
    by_number = {candidate.pr.number: candidate for candidate in candidates}
    assert 5 not in by_number  # bot PRs never become candidates (PRD §70)
    assert by_number[3].rejection_code is RejectionCode.NO_INSTRUCTION


def test_mine_candidates_without_test_requirement(mining_repo: Path) -> None:
    cfg = CFG.model_copy(update={"require_test_change": False})
    candidates = mine_candidates(GitRepo(mining_repo), cfg, enrich=_text_enricher)
    by_number = {candidate.pr.number: candidate for candidate in candidates}
    assert by_number[2].status is TaskStatus.DISCOVERED  # impl-only PR now passes
    assert by_number[2].assessment.task_type is TaskType.FEATURE


def test_mine_candidates_history_unsupported(mining_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gr = GitRepo(mining_repo)
    ghost = PRInfo(number=99, merge_sha="0" * 40)  # base/head shas missing
    monkeypatch.setattr(GitRepo, "merged_prs", lambda self, lookback_days, now=None: [ghost])
    candidates = mine_candidates(gr, CFG)
    assert len(candidates) == 1
    assert candidates[0].status is TaskStatus.FILTERED
    assert candidates[0].rejection_code is RejectionCode.HISTORY_UNSUPPORTED
    assert candidates[0].assessment.subsystem == "unknown"


# ------------------------------------------------------- adaptive mining (PRD v0.9)

ADAPTIVE_CFG = TaskMiningConfig(
    size_policy="adaptive",
    complexity=ComplexityConfig(method="repository_percentile"),
)

DETECTED_PROJECT = ProjectConfig(
    language="python",
    package_manager="uv",
    install_command="uv sync --frozen",
    test_command="python -m pytest",
    regression_command="python -m pytest",
)


def _lines(count: int) -> str:
    return "value = 1\n" * count


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text("demo\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _body_enricher(pr: PRInfo) -> PRInfo:
    """Tier-B instruction for every PR: a clean, problem-oriented body."""
    return pr.model_copy(
        update={
            "title": "fix: correct bulk import totals",
            "body": (
                "Users report bulk import totals drift whenever rows interleave "
                "during import; totals must match the sum of the submitted rows."
            ),
        }
    )


def test_adaptive_discovers_large_pr_and_hard_caps_still_rejects(tmp_path: Path) -> None:
    """PRD §1, §79-80: a 600-LOC PR across 3 files with tests, clean history
    and a real instruction survives adaptive mining as a scored candidate
    (complexity LARGE via the repository's own distribution), while the same
    repository under the default hard_caps still filters it TASK_TOO_LARGE."""
    repo = _init_repo(tmp_path, "large")
    for number, size in ((1, 30), (2, 60), (3, 90), (4, 120)):
        make_pr(
            repo,
            number,
            f"feat/grow-{number}",
            {f"src/a/grow{number}.py": _lines(size)},
            f"add module {number}",
        )
    make_pr(
        repo,
        5,
        "feat/big",
        {
            "src/payments/big1.py": _lines(200),
            "src/payments/big2.py": _lines(200),
            "src/payments/big3.py": _lines(200),
            "tests/test_big1.py": _lines(40),
            "tests/test_big2.py": _lines(34),
        },
        "add bulk importer",
    )
    make_pr(
        repo,
        6,
        "feat/huge",
        {"src/payments/huge.py": _lines(900), "tests/test_huge.py": _lines(5)},
        "add huge exporter",
    )

    adaptive = mine_candidates(
        GitRepo(repo), ADAPTIVE_CFG, enrich=_body_enricher, project_cfg=DETECTED_PROJECT
    )
    by_number = {candidate.pr.number: candidate for candidate in adaptive}

    big = by_number[5]  # 600 impl LOC, 3 files, 2 test files, 74 test LOC
    assert big.status is TaskStatus.DISCOVERED and big.rejection_code is None
    # Hand-computed: 0.25*.75 + 0.25*.80 + 0.20*1.0 + 0.10*.90 + 0.10*1.0
    # + 0.10*0.17 (P83.3) = 0.7945 -> 0.79 -> MEDIUM.
    assert big.assessment.candidate_score == 0.79
    assert big.assessment.candidate_class is CandidateClass.MEDIUM_CONFIDENCE
    assert big.assessment.complexity is Complexity.LARGE  # P83.3 of this repository
    assert big.assessment.scope_percentile == 83.3
    assert big.assessment.behavioral_loc == 600 and big.assessment.generated_loc == 0
    assert big.assessment.languages_touched == ["python"] and big.assessment.dirs_touched == 1
    assert big.assessment.instruction_score == 0.75 and big.assessment.verifier_score == 0.80
    assert big.assessment.risk_flags == ["large_diff", "small_repository_baseline"]
    assert big.assessment.assessment_version == "adaptive-v1"

    # The even bigger neighbor sits at P100: xlarge is a bucket only the
    # repository-relative path can produce (PRD §19).
    assert by_number[6].status is TaskStatus.DISCOVERED
    assert by_number[6].assessment.complexity is Complexity.XLARGE

    # Parity: hard_caps rejects the same PRs by absolute size and never scores.
    hard_caps = mine_candidates(GitRepo(repo), TaskMiningConfig(), enrich=_body_enricher)
    by_number_legacy = {candidate.pr.number: candidate for candidate in hard_caps}
    assert by_number_legacy[5].status is TaskStatus.FILTERED
    assert by_number_legacy[5].rejection_code is RejectionCode.TASK_TOO_LARGE
    assert by_number_legacy[6].rejection_code is RejectionCode.TASK_TOO_LARGE
    assert by_number_legacy[5].assessment.candidate_score is None
    assert by_number_legacy[5].assessment.candidate_class is None
    assert by_number_legacy[5].assessment.assessment_version is None


def test_adaptive_no_implementation_change(tmp_path: Path) -> None:
    """PRD §9: zero implementation LOC changes no implemented behavior — its
    own hard gate in adaptive mode (hard_caps reports TASK_TOO_SMALL instead)."""
    repo = _init_repo(tmp_path, "testonly")
    make_pr(
        repo,
        1,
        "test/only",
        {"tests/test_only.py": "def test_only():\n    assert True\n"},
        "add test",
    )

    adaptive = mine_candidates(GitRepo(repo), ADAPTIVE_CFG, enrich=_body_enricher)
    assert adaptive[0].status is TaskStatus.FILTERED
    assert adaptive[0].rejection_code is RejectionCode.NO_IMPLEMENTATION_CHANGE

    hard_caps = mine_candidates(GitRepo(repo), TaskMiningConfig(), enrich=_body_enricher)
    assert hard_caps[0].rejection_code is RejectionCode.TASK_TOO_SMALL


def test_adaptive_small_pr_is_low_priority_not_rejected(tmp_path: Path) -> None:
    """PRD §37: below min_implementation_loc is LOW_PRIORITY — low priority is
    not invalidity, so no rejection code, and the PR is not scored."""
    repo = _init_repo(tmp_path, "tiny")
    make_pr(
        repo,
        1,
        "feat/tip",
        {"src/payments/tip.py": _lines(5), "tests/test_tip.py": _lines(1)},
        "add tip",
    )

    candidates = mine_candidates(GitRepo(repo), ADAPTIVE_CFG, enrich=_body_enricher)
    tiny = candidates[0]
    assert tiny.status is TaskStatus.LOW_PRIORITY
    assert tiny.rejection_code is None
    assert tiny.assessment.candidate_score is None
    assert tiny.assessment.assessment_version is None


def test_adaptive_extreme_scope_guard(tmp_path: Path) -> None:
    """PRD §39: the emergency cap still filters pathological diffs regardless
    of the adaptive policy (lowered to 10 LOC here to reach it cheaply)."""
    repo = _init_repo(tmp_path, "extreme")
    make_pr(
        repo,
        1,
        "feat/bulk",
        {"src/payments/bulk.py": _lines(30), "tests/test_bulk.py": _lines(1)},
        "add bulk",
    )

    cfg = ADAPTIVE_CFG.model_copy(update={"extreme_scope_loc": 10})
    candidates = mine_candidates(GitRepo(repo), cfg, enrich=_body_enricher)
    assert candidates[0].status is TaskStatus.FILTERED
    assert candidates[0].rejection_code is RejectionCode.TASK_EXTREME_SCOPE


def test_adaptive_experimental_class_is_low_priority(tmp_path: Path) -> None:
    """PRD §24/§37: EXPERIMENTAL candidates stay eligible but out of the
    default validation pool — title-only instruction, tiny tests and a
    multi-package cross-language diff score 0.48 < 0.60."""
    repo = _init_repo(tmp_path, "experimental")
    make_pr(
        repo,
        1,
        "refactor/stuff",
        {
            "pkg_a/mod.py": _lines(15),
            "pkg_b/comp.ts": _lines(15),
            "pkg_c/util.ts": _lines(10),
            "tests/test_mod.py": _lines(2),
        },
        "refactor module structure",
    )

    def title_only(pr: PRInfo) -> PRInfo:
        return pr.model_copy(update={"title": "refactor module structure"})

    candidates = mine_candidates(GitRepo(repo), ADAPTIVE_CFG, enrich=title_only)
    candidate = candidates[0]
    assert candidate.status is TaskStatus.LOW_PRIORITY
    assert candidate.rejection_code is None
    assert candidate.assessment.candidate_score == 0.48
    assert candidate.assessment.candidate_class is CandidateClass.EXPERIMENTAL
    assert candidate.assessment.instruction_score == 0.45  # tier C, title-derived
    assert candidate.assessment.languages_touched == ["python", "typescript"]
    assert candidate.assessment.risk_flags == [
        "cross_language",
        "multi_package",
        "small_repository_baseline",
        "weak_verifier_evidence",
        "xlarge_diff",
    ]


def test_adaptive_repository_percentile_yields_xlarge(tmp_path: Path) -> None:
    """PRD §19: xlarge is a repository-relative bucket — the biggest of 25 PRs
    sits at P100 and becomes xlarge (the absolute path never produces it), and
    the baseline covers gated PRs too: n=25 clears the small-baseline flag."""
    repo = _init_repo(tmp_path, "distribution")
    for number in range(1, 25):
        squash_pr(
            repo,
            number,
            {f"src/pkg/mod{number}.py": _lines(5), f"tests/test_mod{number}.py": _lines(1)},
            "touch module",
        )
    squash_pr(
        repo,
        25,
        {"src/pkg/huge.py": _lines(500), "tests/test_huge.py": _lines(10)},
        "add huge module",
    )

    candidates = mine_candidates(
        GitRepo(repo), ADAPTIVE_CFG, enrich=_body_enricher, project_cfg=DETECTED_PROJECT
    )
    by_number = {candidate.pr.number: candidate for candidate in candidates}

    biggest = by_number[25]
    assert biggest.status is TaskStatus.DISCOVERED
    assert biggest.assessment.complexity is Complexity.XLARGE  # P100 of 25 PRs
    assert biggest.assessment.scope_percentile == 100.0
    assert biggest.assessment.risk_flags == ["xlarge_diff"]

    # A gated sub-floor PR keeps the absolute buckets and stays unscored.
    small = by_number[7]
    assert small.status is TaskStatus.LOW_PRIORITY
    assert small.assessment.complexity is Complexity.SMALL
    assert small.assessment.candidate_score is None
