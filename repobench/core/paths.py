"""Filesystem layout under .repobench/ (PRD §115)."""

from __future__ import annotations

from pathlib import Path


class ProjectPaths:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    @property
    def repobench_dir(self) -> Path:
        return self.root / ".repobench"

    @property
    def state_db(self) -> Path:
        return self.repobench_dir / "state.db"

    @property
    def cache_dir(self) -> Path:
        return self.repobench_dir / "cache"

    @property
    def tasks_dir(self) -> Path:
        return self.repobench_dir / "tasks"

    @property
    def benchmarks_dir(self) -> Path:
        return self.repobench_dir / "benchmarks"

    @property
    def runs_dir(self) -> Path:
        return self.repobench_dir / "runs"

    @property
    def workspaces_dir(self) -> Path:
        return self.repobench_dir / "workspaces"

    def ensure(self) -> None:
        for d in (
            self.repobench_dir,
            self.cache_dir,
            self.tasks_dir,
            self.benchmarks_dir,
            self.runs_dir,
            self.workspaces_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def benchmark_dir(self, benchmark_id: str) -> Path:
        return self.benchmarks_dir / benchmark_id

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id


def find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a Git work tree. None when outside a repository."""
    cur = Path(start).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None
