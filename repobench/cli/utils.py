"""Shared CLI utilities for RepoBench."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from repobench.storage.database import Database
from repobench.utils import get_git_root

console = Console()


def get_database() -> tuple[Database, Path]:
    """Find and open the RepoBench database.

    Looks for .repobench/state.db starting from cwd up to git root.
    Returns (database, repobench_dir).
    Exits with error if not found.
    """
    cwd = Path.cwd()
    git_root = get_git_root(cwd)

    # Search from cwd up to git root
    search_dirs = [cwd]
    if git_root and git_root != cwd:
        p = cwd
        while p != git_root and p != p.parent:
            p = p.parent
            search_dirs.append(p)
        search_dirs.append(git_root)

    for d in search_dirs:
        repobench_dir = d / ".repobench"
        db_path = repobench_dir / "state.db"
        if db_path.exists():
            db = Database(db_path)
            db.initialize()
            return db, repobench_dir

    console.print(
        "[red]Error:[/red] RepoBench not initialized. Run [bold]repobench init[/bold] first."
    )
    sys.exit(1)
