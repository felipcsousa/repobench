"""Environment build and baseline health for task validation."""

from __future__ import annotations

from pathlib import Path

from repobench.config import RepoBenchConfig
from repobench.logging import get_logger
from repobench.utils import run_cmd_safe

log = get_logger("validation.environment")

_UNSUPPORTED_STACK_MESSAGE = (
    "Environment build failed. The project's dependency setup could not be "
    "reconstructed with confidence. Candidate marked: ENVIRONMENT_UNSUPPORTED"
)


def build_environment(
    base_dir: Path,
    config: RepoBenchConfig,
) -> tuple[bool, str]:
    """Build the environment for a task at the given base directory.

    Attempts install based on detected package manager. Returns
    (success, message).
    """
    project = config.project
    install_command = project.install_command

    if not install_command:
        install_command = _detect_install_command(base_dir)

    if not install_command:
        return False, _UNSUPPORTED_STACK_MESSAGE

    log.info("Installing dependencies: %s", install_command)
    success, stdout, stderr = run_cmd_safe(
        _split_command(install_command),
        cwd=base_dir,
        timeout=600,
    )

    if success:
        return True, f"Dependencies installed: {install_command}"
    else:
        return False, _format_error(
            "Environment build failed",
            install_command,
            stderr,
        )


def check_baseline_health(
    base_dir: Path,
    config: RepoBenchConfig,
) -> tuple[bool, str]:
    """Check that the base state is healthy (tests pass before the task)."""
    test_command = config.project.test_command
    if not test_command:
        test_command = _detect_test_command(base_dir)

    if not test_command:
        return True, "No test command configured; baseline health skipped"

    log.info("Running baseline tests: %s", test_command)
    success, stdout, stderr = run_cmd_safe(
        _split_command(test_command),
        cwd=base_dir,
        timeout=600,
    )

    if success:
        return True, "Baseline tests pass"
    else:
        return False, _format_error(
            "Baseline health check failed. The repository may have been "
            "broken at this commit. Candidate marked: BASELINE_BROKEN",
            test_command,
            stderr,
        )


def _detect_install_command(base_dir: Path) -> str | None:
    """Detect install command from project files."""
    from repobench.repository.detection import detect_package_manager

    pkg_manager = detect_package_manager(base_dir)
    if pkg_manager == "pnpm":
        return "pnpm install --frozen-lockfile"
    if pkg_manager == "yarn":
        return "yarn install --frozen-lockfile"
    if pkg_manager == "npm":
        return "npm ci"
    if pkg_manager == "bun":
        return "bun install --frozen-lockfile"
    if pkg_manager == "uv":
        return "uv sync"
    if pkg_manager == "poetry":
        return "poetry install --no-interaction"
    if pkg_manager == "pip":
        if (base_dir / "requirements.txt").exists():
            return "pip install -r requirements.txt"
        return "pip install -e ."
    if pkg_manager == "cargo":
        return "cargo fetch"
    if pkg_manager == "go":
        return "go mod download"
    if pkg_manager == "maven":
        return "mvn dependency:resolve -q"
    if pkg_manager == "gradle":
        if (base_dir / "gradlew").exists():
            return "./gradlew dependencies --quiet"
        return "gradle dependencies --quiet"

    return None


def _detect_test_command(base_dir: Path) -> str | None:
    """Detect test command from project files."""
    from repobench.repository.detection import detect_test_framework

    framework = detect_test_framework(base_dir)
    if framework == "pytest":
        return "python -m pytest"
    if framework == "vitest":
        return "npx vitest run"
    if framework == "jest":
        return "npx jest"
    if framework == "playwright":
        return "npx playwright test"
    if framework == "mocha":
        return "npx mocha"
    if framework == "go-test":
        return "go test ./..."
    if framework in ("junit-maven", "testng-maven", "maven-test"):
        return "mvn test -q"
    if framework in ("junit-gradle", "testng-gradle", "gradle-test"):
        if (base_dir / "gradlew").exists():
            return "./gradlew test --quiet"
        return "gradle test --quiet"

    return None


def _split_command(command: str) -> list[str]:
    """Split a shell command string into arguments (simple split)."""
    import shlex

    return shlex.split(command)


def _format_error(what: str, command: str, stderr: str) -> str:
    """Format an error message following the RepoBench error philosophy."""
    snippet = (stderr or "").strip().splitlines()
    detail = snippet[-3:] if snippet else ["(no output)"]
    return f"{what}\n\nCommand:\n{command}\n\nReason:\n{chr(10).join(detail)}\n"
