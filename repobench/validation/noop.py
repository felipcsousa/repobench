"""No-op validation: BASE + verifier patch must FAIL."""

from __future__ import annotations

from pathlib import Path

from repobench.logging import get_logger
from repobench.utils import run_cmd_safe

log = get_logger("validation.noop")


def validate_noop(
    base_dir: Path,
    verifier_files: list[str],
    test_command: str,
) -> tuple[bool, str]:
    """Run the verifier against BASE without the implementation patch.

    Expected result: FAIL (the new tests should not pass without the
    implementation change).

    Returns (noop_fails, message).
    """
    if not test_command:
        return True, "No test command; no-op check skipped (cannot verify)"

    if not verifier_files:
        return True, "No verifier files; no-op check trivially passes (not meaningful)"

    # Build a targeted test command using the verifier files when possible
    command = _targeted_test_command(test_command, verifier_files)

    log.info("No-op validation: %s", command)
    success, stdout, stderr = run_cmd_safe(command, cwd=base_dir, timeout=600)

    if success:
        # Tests PASSED on BASE + verifier without implementation -> invalid
        log.warning("No-op validation FAILED: verifier passes without implementation")
        return False, (
            "REJECTED: VERIFIER_DOES_NOT_DISTINGUISH_SOLUTION. "
            "The new tests pass without the implementation change."
        )
    else:
        # Tests failed as expected
        log.info("No-op validation passed: verifier fails without implementation")
        return True, "No-op validation passed (verifier fails on BASE)"


def _targeted_test_command(test_command: str, verifier_files: list[str]) -> list[str]:
    """Build a test command targeting specific verifier files.

    Tries to use pytest/vitest file targeting. Falls back to the full
    test command if targeting is not possible.
    """
    import shlex

    parts = shlex.split(test_command)

    # pytest: python -m pytest tests/foo.py
    if "pytest" in parts:
        target = _find_test_target(base_dir=None, verifier_files=verifier_files, framework="pytest")
        if target:
            return parts + [target]

    # vitest: npx vitest run tests/foo.test.ts
    if "vitest" in parts:
        target = _find_test_target(base_dir=None, verifier_files=verifier_files, framework="vitest")
        if target:
            return parts + [target]

    return parts


def _find_test_target(
    base_dir: Path | None, verifier_files: list[str], framework: str
) -> str | None:
    """Find a single file target from verifier files."""
    if not verifier_files:
        return None
    # Use the first verifier file
    target = verifier_files[0]
    return target
