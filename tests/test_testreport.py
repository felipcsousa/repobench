"""Parser tests for the JUnit report extraction (partial credit, PRD §4).

Pure stdlib parsing under test: every fixture is an inline XML string written to
tmp_path — no pytest invocation, no network, no processes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repobench.execution.testreport import (
    JUNIT_FILENAME,
    TestCounts,
    augmented_argv,
    invokes_pytest,
    parse_junit,
)


def _write_junit(tmp_path: Path, xml: str) -> Path:
    path = tmp_path / JUNIT_FILENAME
    path.write_text(xml)
    return path


# ------------------------------------------------------------------ parse_junit


def test_parse_junit_mixed_outcomes_counts_failures_and_errors(tmp_path: Path) -> None:
    """<failure> and <error> both count as failed; bare testcases are passed."""
    path = _write_junit(
        tmp_path,
        """\
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="5" failures="1" errors="1" skipped="1">
    <testcase classname="tests.calc" name="test_a" time="0.001"/>
    <testcase classname="tests.calc" name="test_b" time="0.001"/>
    <testcase classname="tests.calc" name="test_c" time="0.001">
        <failure message="assert 2 == 6">AssertionError</failure>
    </testcase>
    <testcase classname="tests.calc" name="test_d" time="0.001">
        <error message="boom">ValueError</error>
    </testcase>
    <testcase classname="tests.calc" name="test_e" time="0.001">
        <skipped type="pytest.skip" message="later">not now</skipped>
    </testcase>
</testsuite>
""",
    )

    counts = parse_junit(path)

    assert counts == TestCounts(passed=2, failed=2, skipped=1, total=5)
    assert counts is not None and counts.passed + counts.failed + counts.skipped == counts.total


def test_parse_junit_enumerates_nested_testsuites(tmp_path: Path) -> None:
    """A <testsuites> root with several <testsuite> children aggregates across
    all of them via the single testcase enumeration."""
    path = _write_junit(
        tmp_path,
        """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest" tests="4" failures="1" skipped="1">
    <testsuite name="suite-a" tests="3">
        <testcase classname="a" name="test_one"/>
        <testcase classname="a" name="test_two">
            <failure message="nope">AssertionError</failure>
        </testcase>
        <testcase classname="a" name="test_three">
            <skipped message="why not"/>
        </testcase>
    </testsuite>
    <testsuite name="suite-b" tests="1">
        <testcase classname="b" name="test_four"/>
    </testsuite>
</testsuites>
""",
    )

    assert parse_junit(path) == TestCounts(passed=2, failed=1, skipped=1, total=4)


def test_parse_junit_failure_wins_over_error_and_skipped(tmp_path: Path) -> None:
    """When several outcome children exist on one testcase, failed wins."""
    path = _write_junit(
        tmp_path,
        "<testsuite><testcase name='t'>"
        "<skipped/><error message='e'/><failure message='f'/>"
        "</testcase></testsuite>",
    )

    assert parse_junit(path) == TestCounts(passed=0, failed=1, skipped=0, total=1)


def test_parse_junit_malformed_xml_is_none(tmp_path: Path) -> None:
    """A truncated report returns None — no exception, no invented numbers."""
    path = _write_junit(tmp_path, '<?xml version="1.0"?><testsuite><testcase name="x">')

    assert parse_junit(path) is None


def test_parse_junit_missing_file_is_none(tmp_path: Path) -> None:
    assert parse_junit(tmp_path / "no_such_report.xml") is None


def test_parse_junit_empty_testsuites_is_none(tmp_path: Path) -> None:
    """total == 0 is a collection error, never "zero tests ran"."""
    path = _write_junit(tmp_path, "<testsuites></testsuites>")

    assert parse_junit(path) is None


# ------------------------------------------- hidden-test scoping (R1)


def test_parse_junit_scoped_to_verifier_patch_files(tmp_path: Path) -> None:
    """xunit2 reality (verified against a real forkclaw report): no `file`
    attribute, only classname — module paths derived from classname prefixes
    must match, including class-bearing classnames."""
    path = _write_junit(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="4">
    <testcase classname="tests.test_hidden" name="test_a"/>
    <testcase classname="tests.test_hidden.TestGroup" name="test_b">
        <failure message="assert">AssertionError</failure>
    </testcase>
    <testcase classname="tests.test_existing" name="test_c"/>
    <testcase classname="tests.test_existing" name="test_d"/>
</testsuite>""",
    )
    scoped = frozenset({"tests/test_hidden.py"})

    assert parse_junit(path, scoped) == TestCounts(passed=1, failed=1, skipped=0, total=2)
    # No scope = the whole suite, the pre-scope behavior.
    assert parse_junit(path) == TestCounts(passed=3, failed=1, skipped=0, total=4)


def test_parse_junit_scope_matches_repo_relative_patch_paths(tmp_path: Path) -> None:
    """project.cwd repos: the patch path is repo-relative (apps/backend/...),
    the report's classname is rootdir-relative (tests....) — suffix match."""
    path = _write_junit(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
    <testcase classname="tests.test_api" name="test_ok"/>
    <testcase classname="tests.test_other" name="test_x"/>
</testsuite>""",
    )

    counts = parse_junit(path, frozenset({"apps/backend/tests/test_api.py"}))
    assert counts == TestCounts(passed=1, failed=0, skipped=0, total=1)


def test_parse_junit_scope_uses_file_attribute_when_present(tmp_path: Path) -> None:
    path = _write_junit(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
    <testcase classname="whatever" name="test_a" file="tests/test_hidden.py"/>
    <testcase classname="whatever" name="test_b" file="/abs/ws/verify/tests/test_existing.py"/>
</testsuite>""",
    )

    counts = parse_junit(path, frozenset({"tests/test_hidden.py"}))
    assert counts == TestCounts(passed=1, failed=0, skipped=0, total=1)


def test_parse_junit_scope_with_zero_matches_is_none(tmp_path: Path) -> None:
    """A filter matching nothing cannot produce the hidden-scoped count — None,
    never a silent whole-suite fallback."""
    path = _write_junit(
        tmp_path,
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="1">
    <testcase classname="tests.test_existing" name="test_a"/>
</testsuite>""",
    )

    assert parse_junit(path, frozenset({"tests/test_hidden.py"})) is None
    assert parse_junit(path, frozenset()) is None


# --------------------------------------------------------------- invokes_pytest


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pytest", "-q"], True),
        (["/venv/bin/pytest", "-x"], True),  # basename match
        (["python", "-m", "pytest"], True),
        # Windows venv executable — spelled with a real basename, so the check
        # must hold on every OS the suite runs on (forward slashes are
        # separators on both POSIX and Windows Path).
        ([".venv/Scripts/pytest.exe", "-q"], True),
        (["pytest.exe"], True),
        (["npm", "test"], False),
        (["vitest", "run"], False),
        (["python", "-m", "pytestx"], False),  # near-miss is not pytest
        (["pytestx.exe"], False),  # near-miss executable is not pytest either
    ],
)
def test_invokes_pytest(argv: list[str], expected: bool) -> None:
    assert invokes_pytest(argv) is expected


# -------------------------------------------------------------- augmented_argv


def test_augmented_argv_appends_flag_last(tmp_path: Path) -> None:
    junit = tmp_path / JUNIT_FILENAME

    assert augmented_argv(["pytest", "-q"], junit) == ["pytest", "-q", f"--junitxml={junit}"]


def test_augmented_argv_keeps_user_flag_then_overrides_last(tmp_path: Path) -> None:
    """A user --junitxml stays in place but ours is appended after it: pytest's
    last-wins parsing makes the override deterministic."""
    junit = tmp_path / JUNIT_FILENAME

    argv = augmented_argv(["pytest", "--junitxml=seu.xml"], junit)

    assert argv.index("--junitxml=seu.xml") < argv.index(f"--junitxml={junit}")
    assert argv[-1] == f"--junitxml={junit}"


def test_augmented_argv_returns_new_list(tmp_path: Path) -> None:
    argv = ["pytest", "-q"]

    result = augmented_argv(argv, tmp_path / JUNIT_FILENAME)

    assert result == argv + [f"--junitxml={tmp_path / JUNIT_FILENAME}"]
    assert result is not argv  # input never mutated
