"""Deterministic classification of test / verifier paths (PRD §73).

Used both by candidate mining (does the PR change tests?) and by task packaging
(splitting the diff into implementation patch vs hidden verifier patch).
"""

from __future__ import annotations

import re

_TEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"\.test\.[cm]?[tj]sx?$"),
    re.compile(r"\.spec\.[cm]?[tj]sx?$"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"(^|/)snapshots?/"),
    re.compile(r"(^|/)__snapshots__/"),
    re.compile(r"\.snap$"),
    re.compile(r"(^|/)fixtures?/"),
    re.compile(r"(^|/)testdata?/"),
    re.compile(r"(^|/)cassettes?/"),
    re.compile(r"(^|/)mocks?/"),
)


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(rx.search(p) for rx in _TEST_PATTERNS)


def split_changed_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Returns (implementation_paths, test_paths)."""
    impl: list[str] = []
    tests: list[str] = []
    for p in paths:
        (tests if is_test_path(p) else impl).append(p)
    return impl, tests
