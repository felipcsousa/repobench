"""Workspace snapshot tests: the verification copy must keep workspace-created
virtualenvs runnable.

The forkclaw field test crashed at trial time (not build time) because the
verify snapshot dereferenced `.venv/bin/python` — a symlink to an interpreter
outside the tree — and the copied binary no longer resolved its libpython rpath
(dyld: Library not loaded). snapshot_tree must therefore preserve symlinks,
dereferencing only as a fallback where the platform refuses to create them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repobench.execution.workspace import snapshot_tree


def _make_source(tmp_path: Path) -> Path:
    """A tree shaped like a workspace after `uv sync`: a .venv whose python is a
    symlink to an interpreter living OUTSIDE the tree, plus regular files."""
    source = tmp_path / "repo"
    (source / "apps" / "backend").mkdir(parents=True)
    (source / "apps" / "backend" / "main.py").write_text("print('hi')\n")
    interpreter = tmp_path / "cpython-install" / "bin" / "python3.11"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    venv_bin = source / "apps" / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    try:
        os.symlink(interpreter, venv_bin / "python")
    except OSError as exc:  # pragma: no cover - Windows without Developer Mode
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")
    # The source's own git history must never reach the snapshot.
    (source / ".git").mkdir()
    (source / ".git" / "real-history").write_text("secret\n")
    return source


def test_snapshot_preserves_symlinks(tmp_path: Path) -> None:
    """The copied `.venv/bin/python` stays a symlink to the outside interpreter —
    the layout uv/pip create and the one the verifier must be able to run."""
    source = _make_source(tmp_path)
    dest = tmp_path / "verify"

    snapshot_tree(source, dest)

    copied = dest / "apps" / "backend" / ".venv" / "bin" / "python"
    assert copied.is_symlink()
    assert copied.resolve() == (tmp_path / "cpython-install" / "bin" / "python3.11")
    assert (dest / "apps" / "backend" / "main.py").read_text() == "print('hi')\n"
    # .git in the destination is the fresh synthetic repo, never the source's.
    assert (dest / ".git").is_dir()
    assert not (dest / ".git" / "real-history").exists()


def test_snapshot_falls_back_to_dereference_without_symlink_privilege(
    tmp_path: Path, monkeypatch
) -> None:
    """When the platform refuses to create symlinks (Windows outside Developer
    Mode), the snapshot degrades to the dereferencing copy instead of crashing."""
    source = _make_source(tmp_path)
    dest = tmp_path / "verify"
    refused: list[str] = []

    def refuse_symlink(target, link, *args, **kwargs):  # pragma: no cover - raise path
        refused.append(link)
        raise OSError("no privilege to create symlinks")

    monkeypatch.setattr(os, "symlink", refuse_symlink)

    snapshot_tree(source, dest)

    assert refused, "the preserving copy must have tried to create the symlink"
    copied = dest / "apps" / "backend" / ".venv" / "bin" / "python"
    assert not copied.is_symlink()
    assert copied.read_text() == "#!/bin/sh\n"
