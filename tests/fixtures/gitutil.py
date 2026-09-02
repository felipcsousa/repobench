"""Shared git scaffolding for the test suite: the canonical `git()` helper, the
buggy calculator-module constants, and parameterized repo/PR builders.

Single home for the scaffolding previously forked across fixture_repo.py,
test_tasks.py, test_validation.py, test_mining.py and test_repository.py.
Import directly (`from gitutil import ...`) — conftest.py puts this directory
on sys.path. All commands run with an inline -c identity, never global config.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from repobench.core.types import (
    Assessment,
    CandidateInfo,
    Complexity,
    PRInfo,
    TaskType,
)

GIT_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t")

# ------------------------------------------------------------- module fixtures


CALCULATOR_BUGGY = '''def sum_even(numbers):
    """Sum the even numbers in the list."""
    total = 0
    for x in numbers:
        if x % 2 == 1:  # bug: picks odd numbers
            total += x
    return total


def multiply(a, b):
    return a * b
'''

CALCULATOR_FIXED = CALCULATOR_BUGGY.replace("x % 2 == 1", "x % 2 == 0")

# ~22 lines of implementation so a PR carrying it clears the default 20-LOC mining floor.
STATS_MODULE = '''def mean(values):
    if not values:
        raise ValueError("mean of empty list")
    return sum(values) / len(values)


def median(values):
    if not values:
        raise ValueError("median of empty list")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def clamp(value, low, high):
    return max(low, min(high, value))
'''

TEST_CALC = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calculator import multiply


def test_multiply():
    assert multiply(3, 4) == 12
'''

TEST_SUM_EVEN = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calculator import sum_even


def test_sum_even_adds_only_even_numbers():
    assert sum_even([1, 2, 3, 4]) == 6
'''

TEST_MULTIPLY = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calculator import multiply


def test_multiply_is_correct():
    assert multiply(2, 5) == 10
'''

PYPROJECT = '''[project]
name = "calc-fixture"
version = "0.1.0"
requires-python = ">=3.12"
'''

CALC_README = "# calc\n\nA tiny calculator fixture.\n"

# ---------------------------------------------------------------- git plumbing


def git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`; fails the test on a non-zero exit."""
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    """Stage everything, commit, and return the commit sha."""
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def merge_pr(repo: Path, number: int, branch: str) -> str:
    """Merge `branch` into main with a GitHub-style merge subject; returns the merge sha."""
    git(repo, "checkout", "--quiet", "main")
    git(
        repo,
        "merge",
        "--quiet",
        "--no-gpg-sign",
        "--no-ff",
        "-m",
        f"Merge pull request #{number} from {branch}",
        branch,
    )
    return git(repo, "rev-parse", "HEAD")


def make_pr(
    repo: Path, number: int, branch: str, files: dict[str, str], message: str
) -> str:
    """Create a feature branch, commit `files`, merge it GitHub-style, delete the
    branch. Returns the merge sha."""
    git(repo, "checkout", "--quiet", "-b", branch)
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    commit_all(repo, message)
    merge_sha = merge_pr(repo, number, branch)
    git(repo, "branch", "--delete", "--force", branch)
    return merge_sha


def squash_pr(repo: Path, number: int, files: dict[str, str], subject: str) -> str:
    """GitHub squash-style "merge": commit `files` straight onto main with the
    squash subject convention `<subject> (#N)` — a single-parent commit, no
    merge commit exists. Returns the squashed commit sha."""
    git(repo, "checkout", "--quiet", "main")
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return commit_all(repo, f"{subject} (#{number})")


# ------------------------------------------------------ calculator-fixture repo


def build_repo(
    root: Path,
    *,
    number: int = 9,
    fix_bug: bool = True,
    test_name: str = "test_sum_even.py",
    test_source: str | None = TEST_SUM_EVEN,
    readme: str = CALC_README,
    extra_files: dict[str, str] | None = None,
    pr_message: str = "pr change",
) -> dict:
    """Base commit (buggy calculator, passing test, README) plus one PR branch that
    fixes (or merely touches) the implementation and optionally adds a hidden test,
    merged with --no-ff so a real merge commit exists.

    Knobs: PR `number`, `fix_bug` or not, `test_name`/`test_source` (test_source=None
    adds no test file), `readme`, `extra_files` written inside the PR branch.
    Returns {"repo", "number", "base_sha", "head_sha", "merge_sha"}.
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "calculator.py").write_text(CALCULATOR_BUGGY)
    (repo / "test_calc.py").write_text(TEST_CALC)
    (repo / "README.md").write_text(readme)
    base_sha = commit_all(repo, "initial commit")

    branch = f"feat/fix-{number}"
    git(repo, "checkout", "--quiet", "-b", branch)
    if fix_bug:
        (repo / "calculator.py").write_text(CALCULATOR_FIXED)
    else:
        (repo / "calculator.py").write_text(
            CALCULATOR_BUGGY + "\n# touched by the PR without fixing anything\n"
        )
    for name, content in (extra_files or {}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if test_source is not None:
        tests_dir = repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / test_name).write_text(test_source)
    head_sha = commit_all(repo, pr_message)
    merge_sha = merge_pr(repo, number, branch)
    return {
        "repo": repo,
        "number": number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_sha": merge_sha,
    }


def make_candidate(fx: dict) -> CandidateInfo:
    """CandidateInfo matching a `build_repo` fixture: PR #<n> fixing sum_even."""
    number = fx["number"]
    return CandidateInfo(
        candidate_id=f"c_{number}_{fx['base_sha'][:8]}",
        pr=PRInfo(
            number=number,
            title="sum_even adds odd numbers instead of even ones",
            body="Reported by a user: sum_even([1, 2, 3, 4]) returns 4, expected 6.",
            base_sha=fx["base_sha"],
            head_sha=fx["head_sha"],
            merge_sha=fx["merge_sha"],
            merged_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc),
        ),
        assessment=Assessment(
            task_type=TaskType.BUGFIX,
            subsystem="math",
            complexity=Complexity.SMALL,
            language="python",
            instruction="sum_even([1, 2, 3, 4]) returns 4; the function must sum only the even numbers (expected 6).",
            instruction_confidence="B",
            instruction_source="pr_body",
            implementation_loc=2,
            test_loc=7,
            implementation_files=1,
            test_files=1,
        ),
    )


def build_empty_base_repo(root: Path, *, number: int = 11) -> dict:
    """Repository whose first mined PR adds the whole repository (issue #33): the
    merge base is the initial commit, whose tree is empty, so its side of the PR
    diff is everything. A second, healthy PR fixes sum_even on top, mirroring
    `build_repo`. Returns {"repo", "empty_fx", "healthy_fx"} where both fx dicts
    match the `make_candidate` shape."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "--allow-empty", "-m", "initial commit")
    empty_base = git(repo, "rev-parse", "HEAD")

    # PR #<number>: adds the whole repository — base is the empty-tree initial commit.
    branch = f"feat/add-repo-{number}"
    git(repo, "checkout", "--quiet", "-b", branch)
    (repo / "calculator.py").write_text(CALCULATOR_BUGGY)
    (repo / "test_calc.py").write_text(TEST_CALC)
    (repo / "README.md").write_text(CALC_README)
    (repo / "pyproject.toml").write_text(PYPROJECT)
    empty_head = commit_all(repo, "add the calculator project")
    empty_merge = merge_pr(repo, number, branch)

    # PR #<number+1>: the usual healthy bugfix (its base tree is no longer empty).
    fix_branch = f"feat/fix-{number + 1}"
    git(repo, "checkout", "--quiet", "-b", fix_branch)
    (repo / "calculator.py").write_text(CALCULATOR_FIXED)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sum_even.py").write_text(TEST_SUM_EVEN)
    healthy_head = commit_all(repo, "sum_even returns incorrect totals for mixed input")
    healthy_merge = merge_pr(repo, number + 1, fix_branch)

    def fx(n: int, base_sha: str, head_sha: str, merge_sha: str) -> dict:
        return {
            "repo": repo,
            "number": n,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "merge_sha": merge_sha,
        }

    return {
        "repo": repo,
        "empty_fx": fx(number, empty_base, empty_head, empty_merge),
        "healthy_fx": fx(number + 1, empty_merge, healthy_head, healthy_merge),
    }
