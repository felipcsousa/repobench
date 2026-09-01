"""On-disk task package layout (PRD §36).

A task package directory contains exactly: base.tar, instruction.md, gold.patch,
verifier.patch and metadata.json.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from repobench.core.types import PACKAGE_FILES, TaskMetadata, TaskPackage

__all__ = ["PACKAGE_FILES", "write_package"]


def write_package(
    directory: Path,
    *,
    base_tar: Path,
    instruction: str,
    gold_patch: str,
    verifier_patch: str,
    metadata: TaskMetadata,
) -> TaskPackage:
    """Write all package files into `directory` and return the TaskPackage handle.

    The base archive is copied into place (skipped when it already lives there);
    instruction/patches/metadata are written from the given content.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    base_tar = Path(base_tar)
    dest_tar = directory / "base.tar"
    if base_tar.exists():
        if base_tar.resolve() != dest_tar.resolve():
            shutil.copy2(base_tar, dest_tar)
    else:
        raise FileNotFoundError(f"base archive not found: {base_tar}")

    (directory / "instruction.md").write_text(instruction)
    (directory / "gold.patch").write_text(gold_patch)
    (directory / "verifier.patch").write_text(verifier_patch)
    (directory / "metadata.json").write_text(metadata.model_dump_json(indent=2))

    return TaskPackage(task_id=metadata.task_id, directory=directory, metadata=metadata)
