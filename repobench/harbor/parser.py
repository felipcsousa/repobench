"""Harbor result parser: convert Harbor results to RepoBench trial format."""

from __future__ import annotations

import json
from pathlib import Path

from repobench.logging import get_logger
from repobench.models import Trial, VerifierResult

log = get_logger("harbor.parser")


def parse_harbor_result_file(result_path: Path) -> dict:
    """Parse a Harbor result.json file and return raw data.

    Returns a dict with keys: solved, reward, duration_ms, prompt_tokens,
    completion_tokens, cost_usd, error.
    """
    if not result_path.exists():
        return {"solved": False, "error": f"File not found: {result_path}"}

    try:
        data = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"solved": False, "error": f"Failed to parse {result_path}: {e}"}

    result = {
        "solved": False,
        "reward": 0.0,
        "duration_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
        "error": None,
    }

    # Extract reward
    if "reward" in data:
        result["reward"] = float(data["reward"])
        result["solved"] = result["reward"] >= 1.0
    elif "result" in data:
        r = data["result"]
        if isinstance(r, dict):
            result["reward"] = float(r.get("reward", 0))
            result["solved"] = result["reward"] >= 1.0
        elif isinstance(r, (int, float)):
            result["reward"] = float(r)
            result["solved"] = result["reward"] >= 1.0

    if "success" in data:
        result["solved"] = bool(data["success"])
        if result["solved"]:
            result["reward"] = 1.0

    # Extract duration
    for key in ("duration_ms", "duration", "elapsed_ms", "wall_time_ms"):
        if key in data and data[key]:
            val = data[key]
            if isinstance(val, (int, float)):
                result["duration_ms"] = int(val) if val > 1000 else int(val * 1000)
            break

    # Extract token counts
    for key in ("prompt_tokens", "input_tokens", "total_prompt_tokens"):
        if key in data and data[key]:
            result["prompt_tokens"] = int(data[key])
            break

    for key in ("completion_tokens", "output_tokens", "total_completion_tokens"):
        if key in data and data[key]:
            result["completion_tokens"] = int(data[key])
            break

    # Extract cost
    for key in ("cost_usd", "cost", "total_cost"):
        if key in data and data[key]:
            result["cost_usd"] = float(data[key])
            break

    return result


def build_trial_from_harbor_result(
    harbor_result: dict,
    trial_id: str,
    run_id: str,
    benchmark_id: str,
    task_id: str,
    agent_config: str,
) -> Trial:
    """Build an RepoBench Trial from parsed Harbor result data."""
    return Trial(
        trial_id=trial_id,
        run_id=run_id,
        benchmark_id=benchmark_id,
        task_id=task_id,
        agent_config=agent_config,
        solved=harbor_result.get("solved", False),
        duration_ms=harbor_result.get("duration_ms"),
        prompt_tokens=harbor_result.get("prompt_tokens"),
        completion_tokens=harbor_result.get("completion_tokens"),
        cost_usd=harbor_result.get("cost_usd"),
        verifier=VerifierResult(
            task=harbor_result.get("solved", False),
            regression=None,
        ),
        error=harbor_result.get("error"),
    )
