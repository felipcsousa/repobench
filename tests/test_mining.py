"""Mining tests: task-type classification, complexity buckets, subsystem inference,
instruction provenance, and end-to-end candidate mining over temporary git repos."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fixtures.gitutil import git, make_pr
from repobench.config import TaskMiningConfig
from repobench.core.types import Complexity, IssueInfo, PRInfo, RejectionCode, TaskStatus, TaskType
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
