"""Oracle validation: BASE + implementation patch + verifier patch must PASS."""

from __future__ import annotations

import subprocess
from pathlib import Path

from repobench.logging import get_logger
from repobench.utils import run_cmd_safe

log = get_logger("validation.oracle")


def validate_oracle(
    base_dir: Path,
    impl_patch: str,
    verifier_files: list[str],
    test_command: str,
) -> tuple[bool, str]:
    """Apply the gold implementation patch and run the verifier.

    Expected result: PASS.

    Returns (passes, message).
    """
    if not impl_patch:
        return False, "REJECTED: GOLD_DOES_NOT_PASS. No implementation patch provided."

    # Apply the implementation patch
    applied = _apply_patch(base_dir, impl_patch)
    if not applied:
        return False, (
            "REJECTED: GOLD_DOES_NOT_PASS. "
            "The gold implementation patch failed to apply to the base state."
        )

    if not test_command:
        return True, "No test command; oracle check skipped"

    # Run the verifier tests
    import shlex

    parts = shlex.split(test_command)

    # Target verifier files if possible
    if verifier_files and ("pytest" in parts or "vitest" in parts):
        target = verifier_files[0]
        parts = parts + [target]

    log.info("Oracle validation: %s", " ".join(parts))
    success, stdout, stderr = run_cmd_safe(parts, cwd=base_dir, timeout=600)

    if success:
        log.info("Oracle validation passed: gold implementation passes verifier")
        return True, "Oracle validation passed (gold passes)"
    else:
        log.warning("Oracle validation FAILED: gold does not pass verifier")
        return False, _format_gold_failure(stderr)


def _apply_patch(base_dir: Path, patch: str) -> bool:
    """Apply a patch to the base directory using ``git apply``.

    Uses ``git apply --whitespace=nowarn`` for best-effort application.
    """
    if not patch.strip():
        return False

    try:
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch,
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True

        # Fallback: try with --3way if the repo allows it
        result2 = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=nowarn", "-"],
            input=patch,
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result2.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _format_gold_failure(stderr: str) -> str:
    """Format the gold failure message."""
    snippet = (stderr or "").strip().splitlines()
    detail = snippet[-5:] if snippet else ["(no output)"]
    return (
        "REJECTED: GOLD_DOES_NOT_PASS. "
        "The gold implementation does not pass the verifier.\n\n"
        "Test output:\n" + "\n".join(detail)
    )
