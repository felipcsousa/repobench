"""Determinism validation: verifier must pass 3/3 runs."""

from __future__ import annotations

import shlex
from pathlib import Path

from repobench.logging import get_logger
from repobench.utils import run_cmd_safe

log = get_logger("validation.determinism")


def validate_determinism(
    base_dir: Path,
    verifier_files: list[str],
    test_command: str,
    runs: int = 3,
) -> tuple[bool, str]:
    """Run the verifier multiple times and check for consistent results.

    Expected: PASS x runs.

    Returns (deterministic, message).
    """
    if not test_command:
        return True, "No test command; determinism check skipped"

    if runs < 2:
        runs = 3  # Determinism requires at least 2 runs to be meaningful

    parts = shlex.split(test_command)
    if verifier_files and ("pytest" in parts or "vitest" in parts):
        target = verifier_files[0]
        parts = parts + [target]

    results: list[bool] = []
    for i in range(runs):
        log.info("Determinism run %d/%d: %s", i + 1, runs, " ".join(parts))
        success, stdout, stderr = run_cmd_safe(parts, cwd=base_dir, timeout=600)
        results.append(success)

    passed = sum(1 for r in results if r)
    log.info("Determinism results: %d/%d passed", passed, runs)

    if passed == runs:
        return True, f"Determinism validation passed ({passed}/{runs})"
    else:
        return False, (
            f"REJECTED: FLAKY_VERIFIER. Verifier results inconsistent: "
            f"{passed}/{runs} runs passed. Expected {runs}/{runs}."
        )
