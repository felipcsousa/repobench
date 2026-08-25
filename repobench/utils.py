"""Shared utilities for RepoBench."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    capture: bool = True,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command and return the result."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=check,
    )
    return result


def run_cmd_safe(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
) -> tuple[bool, str, str]:
    """Run a command, returning (success, stdout, stderr)."""
    try:
        result = run_cmd(cmd, cwd=cwd, check=False, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except FileNotFoundError:
        return False, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return False, "", str(e)


def is_git_repo(path: Path) -> bool:
    success, _, _ = run_cmd_safe(["git", "rev-parse", "--git-dir"], cwd=path)
    return success


def get_git_root(path: Path) -> Path | None:
    success, stdout, _ = run_cmd_safe(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if success:
        return Path(stdout.strip())
    return None


def has_github_remote(path: Path) -> bool:
    success, stdout, _ = run_cmd_safe(["git", "remote"], cwd=path)
    if success:
        return "origin" in stdout
    return False


def get_github_owner_repo(path: Path) -> tuple[str, str] | None:
    success, stdout, _ = run_cmd_safe(["git", "remote", "get-url", "origin"], cwd=path)
    if not success:
        return None
    url = stdout.strip()
    # Handle both SSH and HTTPS URLs
    if url.startswith("git@github.com:"):
        parts = url.replace("git@github.com:", "").replace(".git", "").split("/")
    elif "github.com" in url:
        parts = url.split("github.com/")[-1].replace(".git", "").split("/")
    else:
        return None
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None
