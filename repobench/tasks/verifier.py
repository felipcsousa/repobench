"""Test file detection and gold/verifier diff splitting."""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from repobench.logging import get_logger

log = get_logger("tasks.verifier")

# Default test file globs (Python + JS/TS)
DEFAULT_TEST_GLOBS = [
    # Python
    "test_*.py",
    "*_test.py",
    "tests/**",
    "tests/**/*.py",
    # JavaScript/TypeScript
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
    "__tests__/**",
    # Go
    "*_test.go",
    "**/testdata/**",
    # Java
    "*Test.java",
    "*Tests.java",
    "*IT.java",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*IT.java",
    "**/src/test/**",
]

# Verifier asset globs
DEFAULT_VERIFIER_ASSET_GLOBS = [
    "test/fixtures/**",
    "**/__snapshots__/**",
    "tests/fixtures/**",
    "**/__mocks__/**",
    # Java
    "**/src/test/resources/**",
]


def detect_test_files(
    changed_files: list[str],
    test_globs: list[str] | None = None,
) -> list[str]:
    """Identify test files from a list of changed files.

    Uses configurable glob patterns to match test files.
    """
    globs = test_globs or DEFAULT_TEST_GLOBS
    test_files: list[str] = []

    for filepath in changed_files:
        if _matches_any_glob(filepath, globs):
            test_files.append(filepath)

    return test_files


def detect_verifier_assets(
    changed_files: list[str],
    asset_globs: list[str] | None = None,
) -> list[str]:
    """Identify verifier asset files (fixtures, snapshots, etc.)."""
    globs = asset_globs or DEFAULT_VERIFIER_ASSET_GLOBS
    assets: list[str] = []

    for filepath in changed_files:
        if _matches_any_glob(filepath, globs):
            assets.append(filepath)

    return assets


def split_gold_verifier(
    pr_diff: str,
    test_files: list[str],
    test_globs: list[str] | None = None,
) -> tuple[str, str]:
    """Split a unified diff into implementation and verifier patches.

    The verifier patch contains changes to test files, snapshots, and
    fixtures. The implementation patch contains everything else.

    Returns (implementation_patch, verifier_patch).
    """
    if not pr_diff:
        return "", ""

    globs = test_globs or DEFAULT_TEST_GLOBS
    all_asset_globs = DEFAULT_VERIFIER_ASSET_GLOBS

    # Parse the diff into per-file chunks
    file_diffs = _parse_diff_files(pr_diff)

    impl_parts: list[str] = []
    verifier_parts: list[str] = []

    for filepath, diff_chunk in file_diffs.items():
        is_test = _matches_any_glob(filepath, globs) or filepath in test_files
        is_asset = _matches_any_glob(filepath, all_asset_globs)

        if is_test or is_asset:
            verifier_parts.append(diff_chunk)
        else:
            impl_parts.append(diff_chunk)

    implementation_patch = "\n".join(impl_parts)
    verifier_patch = "\n".join(verifier_parts)

    return implementation_patch, verifier_patch


def has_test_change(changed_files: list[str]) -> bool:
    """Quick check if any test files were changed."""
    return bool(detect_test_files(changed_files))


def get_test_command_hint(changed_files: list[str]) -> str | None:
    """Suggest a test command based on changed test file types."""
    python_tests = [f for f in changed_files if f.endswith(".py") and _is_python_test(f)]
    js_tests = [
        f
        for f in changed_files
        if any(
            f.endswith(ext)
            for ext in [
                ".test.ts",
                ".test.tsx",
                ".test.js",
                ".test.jsx",
                ".spec.ts",
                ".spec.tsx",
                ".spec.js",
                ".spec.jsx",
            ]
        )
    ]
    go_tests = [f for f in changed_files if f.endswith("_test.go")]
    java_tests = [
        f
        for f in changed_files
        if any(f.endswith(ext) for ext in ["Test.java", "Tests.java", "IT.java"])
        or "/src/test/" in f
    ]

    hints = []
    if python_tests:
        hints.append("pytest")
    if js_tests:
        hints.append("vitest")
    if go_tests:
        hints.append("go test ./...")
    if java_tests:
        # Detect build system
        has_maven = any("pom.xml" in f for f in changed_files) or any(
            "/src/" in f for f in java_tests
        )
        hints.append("mvn test" if has_maven else "gradle test")

    return " && ".join(hints) if hints else None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _matches_any_glob(filepath: str, globs: list[str]) -> bool:
    """Check if a filepath matches any of the given glob patterns."""
    for pattern in globs:
        if fnmatch.fnmatch(filepath, pattern):
            return True
        # Also try matching the basename
        basename = PurePosixPath(filepath).name
        if fnmatch.fnmatch(basename, pattern):
            return True
        # Handle ** patterns (globstar)
        if "**" in pattern:
            # Remove leading/trailing ** and check if the remaining
            # segments appear as a subpath in the filepath
            cleaned = pattern
            while "**" in cleaned:
                cleaned = cleaned.replace("**/", "").replace("/**", "")
            cleaned = cleaned.strip("/")
            if not cleaned:
                # Pattern is just ** — matches everything
                return True
            # Check if cleaned segments appear in filepath
            if cleaned in filepath or filepath.endswith(cleaned) or filepath.startswith(cleaned):
                return True
    return False


def _parse_diff_files(diff_text: str) -> dict[str, str]:
    """Parse a unified diff into per-file chunks.

    Returns {filepath: diff_chunk} mapping.
    """
    files: dict[str, str] = {}
    current_file = None
    current_lines: list[str] = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if current_file and current_lines:
                files[current_file] = "\n".join(current_lines)
            # Extract file path: "diff --git a/path b/path"
            match = re.match(r"diff --git a/(.*?) b/", line)
            if match:
                current_file = match.group(1)
            else:
                current_file = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_file and current_lines:
        files[current_file] = "\n".join(current_lines)

    return files


def _is_python_test(filepath: str) -> bool:
    """Check if a Python file is a test file."""
    name = PurePosixPath(filepath).name
    parts = PurePosixPath(filepath).parts
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if "tests" in parts or "test" in parts:
        return True
    return False
