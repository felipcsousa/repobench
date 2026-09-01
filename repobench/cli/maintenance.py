"""Run inspection and .repobench/ housekeeping: `repobench runs` and `repobench clean`
(issues #4, #9).

Views are read-only aggregations over the runs/trials tables; cleaning is a
two-phase plan/apply so the CLI can default to dry-run without duplicating the
policy in the command layer.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from repobench.analysis.metrics import TargetMetrics, aggregate_trials
from repobench.core.errors import UsageError
from repobench.core.paths import ProjectPaths
from repobench.storage.db import Storage


# ----------------------------------------------------------------- runs views


@dataclass
class RunRowView:
    """One row of `repobench runs` (issue #4)."""

    run_id: str
    benchmark_id: str | None
    status: str
    started_at: str | None
    finished_at: str | None
    targets: int
    trials_done: int
    trials_solved: int


@dataclass
class RunShowView:
    """`repobench runs --show <id>`: run header + per-target summary."""

    row: RunRowView
    targets: list[TargetMetrics]


def list_run_views(storage: Storage) -> list[RunRowView]:
    """Every run with its per-target trial counts, newest first (issue #4)."""
    counts = storage.trials_by_run()
    views: list[RunRowView] = []
    for row in storage.list_runs():
        run_id = row["run_id"]
        per_target = counts.get(run_id, {})
        views.append(
            RunRowView(
                run_id=run_id,
                benchmark_id=row.get("benchmark_id"),
                status=row.get("status") or "—",
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                targets=len(per_target),
                trials_done=sum(t["n"] for t in per_target.values()),
                trials_solved=sum(t["solved"] for t in per_target.values()),
            )
        )
    return views


def show_run_view(storage: Storage, run_id: str) -> RunShowView:
    """Per-target metrics for one run, or a polite error for an unknown id."""
    row = storage.get_run(run_id)
    if row is None:
        known = ", ".join(r["run_id"] for r in storage.list_runs()[:5]) or "none"
        raise UsageError(f"unknown run: {run_id} (recorded runs: {known})")
    views = {view.run_id: view for view in list_run_views(storage)}
    metrics = aggregate_trials(storage.list_trials(run_id))
    return RunShowView(
        row=views[run_id],
        targets=[metrics[tid] for tid in sorted(metrics)],
    )


# ---------------------------------------------------------------------- clean


@dataclass(frozen=True)
class CleanScope:
    """What `repobench clean` may touch, resolved once from CLI flags (issue #9).

    keep_runs semantics: N keeps the N newest runs, 0 drops every run, None
    leaves runs alone — a workspace/cache-only clean never prunes runs implicitly.
    """

    keep_runs: int | None
    workspaces: bool
    cache: bool

    @classmethod
    def from_flags(
        cls,
        runs: int | None = None,
        *,
        workspaces: bool = False,
        cache: bool = False,
        all_scope: bool = False,
    ) -> CleanScope:
        if all_scope:
            workspaces = cache = True
            runs = 0 if runs is None else runs
        if runs is not None:
            if runs < 0:
                raise UsageError("--runs must be >= 0")
            keep: int | None = runs
        elif workspaces or cache:
            keep = None
        else:
            keep = 1  # plain `clean`: conservative preview, prune beyond the newest run
        return cls(keep_runs=keep, workspaces=workspaces, cache=cache)


@dataclass
class CleanPlan:
    """What `repobench clean` would remove (dry-run by default, issue #9)."""

    run_dirs: list[Path]
    run_ids: list[str]  # DB rows (runs + trials) pruned with the dirs
    workspace_dirs: list[Path]
    cache_dir: Path | None
    freed_bytes: int

    @property
    def empty(self) -> bool:
        return not (self.run_dirs or self.workspace_dirs or self.cache_dir)


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def plan_clean(storage: Storage, paths: ProjectPaths, scope: CleanScope) -> CleanPlan:
    """Compute what the scope would remove. Nothing is deleted here."""
    run_dirs: list[Path] = []
    run_ids: list[str] = []
    if scope.keep_runs is not None:
        for row in storage.list_runs()[scope.keep_runs:]:  # newest first
            run_id = row["run_id"]
            run_ids.append(run_id)
            run_dir = paths.run_dir(run_id)
            if run_dir.is_dir():
                run_dirs.append(run_dir)

    workspace_dirs = (
        sorted(p for p in paths.workspaces_dir.iterdir() if p.is_dir())
        if scope.workspaces and paths.workspaces_dir.is_dir()
        else []
    )
    cache_dir = paths.cache_dir if scope.cache and paths.cache_dir.is_dir() else None

    freed = sum(
        _dir_size(d) for d in [*run_dirs, *workspace_dirs, *([cache_dir] if cache_dir else [])]
    )
    return CleanPlan(
        run_dirs=run_dirs,
        run_ids=run_ids,
        workspace_dirs=workspace_dirs,
        cache_dir=cache_dir,
        freed_bytes=freed,
    )


def apply_clean(storage: Storage, plan: CleanPlan) -> None:
    """Execute a computed CleanPlan: rmtree artifacts, prune runs + trials rows."""
    for directory in [*plan.workspace_dirs, *plan.run_dirs]:
        shutil.rmtree(directory, ignore_errors=True)
    if plan.cache_dir is not None:
        shutil.rmtree(plan.cache_dir, ignore_errors=True)
    for run_id in plan.run_ids:
        storage.execute("DELETE FROM trials WHERE run_id = ?", (run_id,))
        storage.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
