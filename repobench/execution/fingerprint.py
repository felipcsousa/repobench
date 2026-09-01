"""Target fingerprints and run reproducibility metadata (PRD §29-31).

Native Mode measures the local configuration, so every trial/run must carry
enough description to attribute results across time: harness versions, a hash
of each target's configuration, and hashes of the repository instruction files
(AGENTS.md/CLAUDE.md/GEMINI.md). Credential CONTENT is never persisted — env
values are reduced to their key names before hashing (PRD §29).
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repobench.core.types import ExecutionTarget

# Repository instruction files whose contents shape harness behavior (PRD §31).
INSTRUCTION_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def target_definition(target: ExecutionTarget) -> dict:
    """Serializable target definition — env values are never included, only keys."""
    return {
        "id": target.id,
        "harness": target.harness,
        "model": target.model,
        "provider": target.provider,
        "args": list(target.args),
        "command": list(target.command) if target.command is not None else None,
        "output": target.output.value,
        "timeout_minutes": target.timeout_minutes,
        "env_keys": sorted(target.env),
    }


def config_hash(definition: dict) -> str:
    """Stable hash over a JSON-serializable definition (sorted keys)."""
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical.encode("utf-8"))


def target_fingerprint(target: ExecutionTarget) -> dict:
    """The PRD §29 fingerprint block for one target."""
    definition = target_definition(target)
    return {"definition": definition, "config_hash": config_hash(definition)}


def instruction_file_hashes(root: Path) -> dict[str, str]:
    """sha256 of every known instruction file present at the repository root."""
    hashes: dict[str, str] = {}
    for name in INSTRUCTION_FILES:
        path = Path(root) / name
        if path.is_file():
            try:
                hashes[name] = _sha256(path.read_bytes())
            except OSError:
                continue  # unreadable file contributes no fingerprint
    return hashes


def build_run_manifest(
    *,
    run_id: str,
    benchmark_id: str | None,
    targets: list[ExecutionTarget],
    harness_versions: dict[str, str | None],
    instruction_hashes: dict[str, str],
    bootstrap_seed: int,
    started_at: str,
    repobench_version: str,
) -> dict:
    """The run-level reproducibility record written to runs/<run-id>/manifest.json
    (PRD §30): a sufficient description of the tested configuration, never a
    bit-level reproducibility promise."""
    return {
        "run_id": run_id,
        "benchmark_id": benchmark_id,
        "repobench_version": repobench_version,
        "python_version": sys.version.split()[0],
        "os": platform.system(),
        "arch": platform.machine(),
        "started_at": started_at,
        "bootstrap_seed": bootstrap_seed,
        "harnesses": dict(harness_versions),
        "instruction_file_hashes": dict(instruction_hashes),
        "targets": [
            {
                **target_fingerprint(target),
                "harness_version": harness_versions.get(target.harness),
            }
            for target in targets
        ],
    }
