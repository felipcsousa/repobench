"""Harbor runner: execute trials via Harbor CLI subprocess."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from repobench.logging import get_logger
from repobench.utils import run_cmd_safe

log = get_logger("harbor.runner")


class HarborTrialResult:
    """Result of a single Harbor trial execution."""

    def __init__(
        self,
        solved: bool = False,
        reward: float = 0.0,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
        stdout: str = "",
        stderr: str = "",
        result_path: Path | None = None,
    ):
        self.solved = solved
        self.reward = reward
        self.duration_ms = duration_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost_usd = cost_usd
        self.error = error
        self.stdout = stdout
        self.stderr = stderr
        self.result_path = result_path


def check_harbor_available() -> tuple[bool, str]:
    """Check if Harbor CLI is available and return version."""
    if not shutil.which("harbor"):
        return False, "harbor not found on PATH"
    ok, out, err = run_cmd_safe(["harbor", "--version"])
    if ok:
        version = out.strip().split("\n")[0] if out.strip() else "unknown"
        return True, version
    return False, err or "harbor --version failed"


def run_harbor_trial(
    task_dir: Path,
    agent: str,
    model: str,
    timeout: int = 600,
    verbose: bool = False,
) -> HarborTrialResult:
    """Execute a single trial via Harbor CLI.

    Args:
        task_dir: Path to the Harbor task directory.
        agent: Agent name (e.g., 'codex', 'claude-code').
        model: Model identifier (e.g., 'openai/gpt-4o').
        timeout: Maximum execution time in seconds.
        verbose: If True, include stdout/stderr in result.

    Returns:
        HarborTrialResult with solved status and metrics.
    """
    cmd = [
        "harbor",
        "run",
        "-p",
        str(task_dir),
        "-a",
        agent,
        "-m",
        model,
    ]

    log.info("Running Harbor: %s", " ".join(cmd))
    start_time = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=task_dir.parent,  # Run from benchmark dir
        )
        duration_ms = int((time.monotonic() - start_time) * 1000)
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if result.returncode != 0:
            log.warning("Harbor exited with code %d", result.returncode)
            return HarborTrialResult(
                solved=False,
                duration_ms=duration_ms,
                error=f"harbor exit code {result.returncode}",
                stdout=stdout if verbose else "",
                stderr=stderr if verbose else "",
            )

        # Try to find and parse result.json
        trial_result = _find_and_parse_result(task_dir, duration_ms)
        if trial_result:
            trial_result.stdout = stdout if verbose else ""
            trial_result.stderr = stderr if verbose else ""
            return trial_result

        # Fallback: check reward.txt directly
        reward_path = task_dir / "logs" / "verifier" / "reward.txt"
        if reward_path.exists():
            try:
                reward_val = float(reward_path.read_text().strip())
                return HarborTrialResult(
                    solved=reward_val >= 1.0,
                    reward=reward_val,
                    duration_ms=duration_ms,
                    stdout=stdout if verbose else "",
                    stderr=stderr if verbose else "",
                )
            except (ValueError, OSError):
                pass

        # No result found — treat as failure
        return HarborTrialResult(
            solved=False,
            duration_ms=duration_ms,
            error="No result.json or reward.txt found",
            stdout=stdout if verbose else "",
            stderr=stderr if verbose else "",
        )

    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return HarborTrialResult(
            solved=False,
            duration_ms=duration_ms,
            error=f"Timeout after {timeout}s",
        )
    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return HarborTrialResult(
            solved=False,
            duration_ms=duration_ms,
            error=str(e),
        )


def _find_and_parse_result(task_dir: Path, duration_ms: int) -> HarborTrialResult | None:
    """Find and parse Harbor result.json near the task directory.

    Harbor stores results in various locations depending on version.
    We search common patterns.
    """
    # Search patterns for result.json
    search_patterns = [
        task_dir / "result.json",
        task_dir.parent / "result.json",
        task_dir.parent / "jobs" / "result.json",
    ]

    # Also glob for any result.json in nearby directories
    for parent in [task_dir, task_dir.parent]:
        if parent.exists():
            for result_file in parent.rglob("result.json"):
                if result_file not in search_patterns:
                    search_patterns.append(result_file)

    for result_path in search_patterns:
        if result_path.exists():
            try:
                return _parse_result_json(result_path, duration_ms)
            except Exception as e:
                log.warning("Failed to parse %s: %s", result_path, e)

    return None


def _parse_result_json(result_path: Path, duration_ms: int) -> HarborTrialResult:
    """Parse a Harbor result.json file."""
    data = json.loads(result_path.read_text())

    # Harbor result.json structure varies, handle common fields
    reward = 0.0
    solved = False

    # Check for reward in various locations
    if "reward" in data:
        reward = float(data["reward"])
        solved = reward >= 1.0
    elif "result" in data:
        result_data = data["result"]
        if isinstance(result_data, dict):
            reward = float(result_data.get("reward", 0))
            solved = reward >= 1.0
        elif isinstance(result_data, (int, float)):
            reward = float(result_data)
            solved = reward >= 1.0

    # Check for success field
    if "success" in data:
        solved = bool(data["success"])
        if solved:
            reward = 1.0

    # Extract metrics
    duration = data.get("duration_ms") or data.get("duration") or duration_ms
    if isinstance(duration, (int, float)) and duration > 1000:
        duration_ms = int(duration)

    prompt_tokens = data.get("prompt_tokens") or data.get("input_tokens")
    completion_tokens = data.get("completion_tokens") or data.get("output_tokens")
    cost_usd = data.get("cost_usd") or data.get("cost")

    return HarborTrialResult(
        solved=solved,
        reward=reward,
        duration_ms=duration_ms,
        prompt_tokens=int(prompt_tokens) if prompt_tokens else None,
        completion_tokens=int(completion_tokens) if completion_tokens else None,
        cost_usd=float(cost_usd) if cost_usd else None,
        result_path=result_path,
    )


def run_harbor_trials_batch(
    task_dirs: list[Path],
    agent: str,
    model: str,
    concurrency: int = 1,
    timeout: int = 600,
    verbose: bool = False,
) -> list[HarborTrialResult]:
    """Run multiple Harbor trials, respecting concurrency limit.

    For concurrency > 1, uses Harbor's built-in parallelism.
    For concurrency = 1, runs sequentially.
    """
    if concurrency <= 1 or len(task_dirs) == 1:
        # Sequential execution
        results = []
        for task_dir in task_dirs:
            result = run_harbor_trial(task_dir, agent, model, timeout, verbose)
            results.append(result)
        return results

    # Parallel: run all tasks in one harbor run command
    if len(task_dirs) == 1:
        return [run_harbor_trial(task_dirs[0], agent, model, timeout, verbose)]

    # For multiple tasks, run harbor with the parent directory
    # Harbor supports -p for a dataset directory containing multiple tasks
    parent_dir = task_dirs[0].parent
    cmd = [
        "harbor",
        "run",
        "-p",
        str(parent_dir),
        "-a",
        agent,
        "-m",
        model,
        "--n-concurrent",
        str(concurrency),
    ]

    log.info("Running Harbor batch: %s", " ".join(cmd))
    start_time = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout * len(task_dirs),  # Scale timeout
            cwd=parent_dir.parent,
        )
        duration_ms = int((time.monotonic() - start_time) * 1000)

        if result.returncode != 0:
            log.warning("Harbor batch exited with code %d", result.returncode)
            return [
                HarborTrialResult(
                    solved=False,
                    duration_ms=duration_ms,
                    error=f"harbor exit code {result.returncode}",
                )
                for _ in task_dirs
            ]

        # Parse individual results
        results = []
        for task_dir in task_dirs:
            r = _find_and_parse_result(task_dir, duration_ms // len(task_dirs))
            if r is None:
                r = HarborTrialResult(
                    solved=False,
                    duration_ms=duration_ms // len(task_dirs),
                    error="No result found",
                )
            results.append(r)

        return results

    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return [
            HarborTrialResult(
                solved=False,
                duration_ms=duration_ms,
                error=f"Timeout after {timeout * len(task_dirs)}s",
            )
            for _ in task_dirs
        ]
    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return [
            HarborTrialResult(solved=False, duration_ms=duration_ms, error=str(e))
            for _ in task_dirs
        ]
