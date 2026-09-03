"""JUnit report extraction for the hidden verifier (partial credit, PRD §4).

Per-test counts (passed/failed/skipped) are extracted from a JUnit XML report
produced by the verifier process itself: `--junitxml` is appended to the
verifier's argv, so the same process writes the report — no extra spawn
(PRD §4.2). The counts are a FINDING registered beside the verdict, never
inside it: SOLVED/UNSOLVED stays exit-code-only (PRD §63), and a report that is
absent, malformed, or records zero tests yields None — numbers are never
invented.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

_LOG = logging.getLogger("repobench.execution.testreport")

# Written at the root of the verification snapshot (PRD §4.2). The snapshot dies
# with the workspace at publish time, so the report must be parsed before
# _verify returns.
JUNIT_FILENAME = ".repobench_junit.xml"


class TestCounts(NamedTuple):
    """Per-test outcome counts extracted from one JUnit report."""

    # Name is fixed by the spec but starts with "Test": keep pytest's collector
    # from treating the imported class as a test class.
    __test__ = False

    passed: int
    failed: int  # failures + errors
    skipped: int
    total: int  # passed + failed + skipped


# Executable spellings whose basename marks a pytest invocation. ".exe" covers
# direct venv-executable calls on Windows (`.venv/Scripts/pytest.exe`).
_PYTEST_BASENAMES = frozenset({"pytest", "pytest.exe"})


def invokes_pytest(argv: list[str]) -> bool:
    """True when the command invokes pytest: any token exactly "pytest" or whose
    basename is a pytest executable — covers `pytest -q`, `/venv/bin/pytest`,
    `.venv/Scripts/pytest.exe` and `python -m pytest`. Non-pytest runners (npm,
    vitest, ...) stay untouched: appending a pytest flag to them would change
    their behavior."""
    return any(
        token == "pytest" or Path(token).name in _PYTEST_BASENAMES for token in argv
    )


def augmented_argv(argv: list[str], junit_path: Path) -> list[str]:
    """`argv` plus `--junitxml=<junit_path>` appended LAST.

    pytest option parsing is last-wins, so a `--junitxml` already present in the
    user's command is deterministically overridden by ours — the report always
    lands at the path the runner will parse. Returns a new list; the input is
    never mutated.
    """
    return [*argv, f"--junitxml={junit_path}"]


def _posix(path_str: str) -> str:
    return Path(path_str).as_posix()


def _testcase_candidates(case: ET.Element) -> list[str]:
    """Paths one testcase could live at, for matching against patch paths.

    Prefers the `file` attribute when the report carries one; pytest's default
    xunit2 family omits it (verified against a real forkclaw report), so module
    paths are also derived from classname prefixes — "tests.test_foo.TestBar"
    yields tests.py and tests/test_foo.py, because trailing dot-parts may be
    classes rather than package segments.
    """
    candidates: list[str] = []
    file_attr = case.get("file")
    if file_attr:
        candidates.append(_posix(file_attr))
    classname = case.get("classname") or ""
    if classname:
        parts = classname.split(".")
        for depth in range(1, len(parts) + 1):
            candidates.append("/".join(parts[:depth]) + ".py")
    return candidates


def _same_file(candidate: str, patch_path: str) -> bool:
    """Boundary-aware suffix match in both directions: the report path may be
    rootdir-relative (shorter than the repo-relative patch path) or absolute
    (longer) — "tests/test_a.py" vs "apps/backend/tests/test_a.py"."""
    left, right = _posix(candidate), _posix(patch_path)
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


def parse_junit(
    path: Path, verifier_paths: frozenset[str] | None = None
) -> TestCounts | None:
    """Per-test counts from a JUnit XML report, or None when the report cannot
    be trusted: absent, unreadable, malformed, or recording zero tests (a
    collection error must never read as "zero tests ran").

    Testcases are enumerated across the whole tree (`iter("testcase")`), so a
    single `<testsuite>` root and nested `<testsuites>` behave identically. A
    testcase counts as failed when it carries a `<failure>` or `<error>` child
    (failure wins when both are present), skipped when it carries `<skipped>`,
    passed otherwise.

    `verifier_paths` scopes the denominator to the hidden tests: only testcases
    living in a file the hidden-verifier patch touches are counted, so a noop
    agent scores 0/N instead of (suite−N)/suite on a mostly-green suite. None
    counts the whole suite (the pre-scope behavior); a filter that matches zero
    testcases also yields None — the hidden-scoped count cannot be computed and
    a number is never invented, so there is no silent fallback to the whole
    suite.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        _LOG.debug("junit report %s not parseable: %s", path, exc)
        return None

    passed = failed = skipped = 0
    for case in root.iter("testcase"):
        if verifier_paths is not None and not any(
            _same_file(candidate, patch_path)
            for candidate in _testcase_candidates(case)
            for patch_path in verifier_paths
        ):
            continue
        children = {child.tag for child in case}
        if "failure" in children or "error" in children:
            failed += 1
        elif "skipped" in children:
            skipped += 1
        else:
            passed += 1
    total = passed + failed + skipped
    if total == 0:
        return None
    return TestCounts(passed=passed, failed=failed, skipped=skipped, total=total)
