"""Immutable benchmark manifests (PRD §88-89).

A benchmark is immutable: the ID is derived from task versions, the sampling
configuration snapshot and the methodology version. A rebuild produces a new
benchmark; an existing one is never silently modified.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pydantic

from repobench.config import BenchmarkConfig
from repobench.core.ids import METHODOLOGY_VERSION, new_benchmark_id, sha256_hex
from repobench.core.types import TaskMetadata, utcnow
from repobench.benchmark.coverage import CoverageReport
from repobench.benchmark.health import HealthReport

MANIFEST_FILENAME = "manifest.json"


class BenchmarkManifest(pydantic.BaseModel):
    benchmark_id: str
    created_at: datetime
    size: int
    task_ids: list[str]
    task_versions: dict[str, int]
    health: HealthReport | None = None
    coverage: CoverageReport | None = None
    config_snapshot: dict
    methodology_version: str
    repository: str | None = None


def build_manifest(
    tasks: list[TaskMetadata],
    health: HealthReport | None,
    coverage: CoverageReport | None,
    config: BenchmarkConfig,
    *,
    repository: str | None = None,
) -> BenchmarkManifest:
    """Build a manifest whose ID is immutable by construction (PRD §89)."""
    config_snapshot = config.model_dump(mode="json")
    # Fingerprint: sorted task_id:version pairs make the ID content-derived.
    fingerprint = "\n".join(sorted(f"{t.task_id}:{t.version}" for t in tasks))
    seed = sha256_hex(fingerprint) + repr(config_snapshot) + METHODOLOGY_VERSION
    return BenchmarkManifest(
        benchmark_id=new_benchmark_id(seed),
        created_at=utcnow(),
        size=len(tasks),
        task_ids=[t.task_id for t in tasks],
        task_versions={t.task_id: t.version for t in tasks},
        health=health,
        coverage=coverage,
        config_snapshot=config_snapshot,
        methodology_version=METHODOLOGY_VERSION,
        repository=repository,
    )


def save_manifest(manifest: BenchmarkManifest, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_FILENAME
    path.write_text(manifest.model_dump_json(indent=2))
    return path


def load_manifest(directory: Path) -> BenchmarkManifest:
    path = Path(directory) / MANIFEST_FILENAME
    return BenchmarkManifest.model_validate_json(path.read_text())


def load_stored_manifest(row: dict) -> BenchmarkManifest | None:
    """Manifest from a benchmarks-table row, or None when absent/unreadable
    (issue #15). Rows store the manifest.json FILE path while load_manifest
    takes the directory — both shapes are accepted here; a missing or corrupt
    manifest degrades to None so callers can fail with a polite error."""
    raw = row.get("manifest_path") if row else None
    if not raw:
        return None
    path = Path(raw)
    if path.is_file():
        path = path.parent
    if not (path / MANIFEST_FILENAME).is_file():
        return None
    try:
        return load_manifest(path)
    except Exception:
        return None
