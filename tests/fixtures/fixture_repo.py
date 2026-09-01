"""Deterministic fixture repository with two merged PRs (PRD §140-141).

PR #7 fixes the buggy `sum_even` AND adds tests/test_sum_even.py — a good
bugfix candidate whose test change becomes the hidden verifier. PR #8 changes
only documentation, so mining must filter it with NO_TEST_CHANGE.

History (all git calls run with an inline -c identity, never global config):

    initial commit (buggy calculator.py, passing test_calc.py, pyproject.toml)
      └─ feat/fix-sum-even: fix + stats helpers + tests/test_sum_even.py
           merge --no-ff -m "Merge pull request #7 from feat/fix-sum-even"
      └─ docs/usage: README-only change
           merge --no-ff -m "Merge pull request #8 from docs/usage"

All scaffolding (git helper, buggy-module constants) lives in gitutil.py.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.gitutil import (
    CALCULATOR_BUGGY,
    CALCULATOR_FIXED,
    PYPROJECT,
    STATS_MODULE,
    TEST_CALC,
    TEST_SUM_EVEN,
    commit_all,
    git,
    merge_pr,
)

README_INITIAL = "# calc-fixture\n\nA tiny calculator used by the RepoBench fixture suite.\n"

README_EXPANDED = README_INITIAL + """
## Usage

Import `calculator.sum_even` for even-number sums and `calculator.multiply`
for products. The helpers in `stats.py` cover basic descriptive statistics.
"""


def build_fixture_repo(dest: Path) -> Path:
    """Create the fixture repository at `dest` (must not exist yet) and return its path."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    repo = dest
    git(repo, "init", "--quiet", "--initial-branch=main")

    # Initial commit: buggy implementation with a passing baseline test.
    (repo / "calculator.py").write_text(CALCULATOR_BUGGY)
    (repo / "test_calc.py").write_text(TEST_CALC)
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "README.md").write_text(README_INITIAL)
    commit_all(repo, "initial commit")

    # PR #7: fix sum_even, add helper module and the hidden verifier test.
    git(repo, "checkout", "--quiet", "-b", "feat/fix-sum-even")
    (repo / "calculator.py").write_text(CALCULATOR_FIXED)
    (repo / "stats.py").write_text(STATS_MODULE)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sum_even.py").write_text(TEST_SUM_EVEN)
    commit_all(repo, "sum_even returns incorrect totals for mixed input")
    merge_pr(repo, 7, "feat/fix-sum-even")

    # PR #8: no test change — must be filtered with NO_TEST_CHANGE.
    git(repo, "checkout", "--quiet", "-b", "docs/usage")
    (repo / "README.md").write_text(README_EXPANDED)
    commit_all(repo, "expand README with usage notes")
    merge_pr(repo, 8, "docs/usage")

    git(repo, "checkout", "--quiet", "main")
    return repo
