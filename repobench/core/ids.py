"""ID generation helpers.

Task and benchmark IDs are deterministic where possible (derived from PR + SHAs);
trial and run IDs are random. Benchmark IDs embed the creation date (PRD §88).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

# Bumped when the task/benchmark methodology changes in a way that breaks comparability.
METHODOLOGY_VERSION = "1"


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def short_hash(*parts: str, length: int = 8) -> str:
    return sha256_hex("\x1f".join(parts))[:length]


def new_candidate_id(pr_number: int | None, base_sha: str) -> str:
    pr = pr_number if pr_number is not None else "na"
    return f"c_{pr}_{short_hash('candidate', str(pr), base_sha)}"


def new_task_id(pr_number: int | None, base_sha: str, gold_sha: str) -> str:
    pr = pr_number if pr_number is not None else "na"
    return f"t_{pr}_{short_hash('task', str(pr), base_sha, gold_sha)}"


def new_benchmark_id(seed: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"rb_b_{day}_{short_hash('benchmark', seed, day, length=4)}"


def new_trial_id() -> str:
    return f"trial_{secrets.token_hex(8)}"


def new_run_id() -> str:
    return f"run_{secrets.token_hex(8)}"
