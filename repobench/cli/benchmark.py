"""RepoBench Benchmark — build and manage benchmarks."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repobench.benchmark.health import calculate_health
from repobench.cli.utils import get_database
from repobench.config import load_config
from repobench.logging import setup_logging
from repobench.models import BenchmarkManifest, CandidateTask
from repobench.utils import get_git_root, get_github_owner_repo

console = Console()


def run_benchmark_build(size: int | None = None, force: bool = False) -> None:
    """Build a representative benchmark from valid candidates."""
    setup_logging()
    db, repobench_dir = get_database()
    git_root = get_git_root(Path.cwd()) or Path.cwd()

    config = load_config(git_root)
    requested_size = size or config.benchmark.size

    # ── Gather valid candidates ───────────────────────────────────────────
    valid = db.get_candidates_by_status("valid")

    if not valid:
        console.print(
            "[red]Error:[/red] No VALID candidates found.\n"
            "Run [bold]repobench candidates[/bold] to check candidate status.\n"
            "Run [bold]repobench task validate <id>[/bold] to validate candidates."
        )
        db.close()
        sys.exit(1)

    if len(valid) < 5:
        console.print(
            f"[yellow]Warning:[/yellow] Only {len(valid)} valid candidates. "
            "Benchmark may have poor representativeness."
        )

    # ── Select tasks (V1: stratified-ish selection) ────────────────────────
    # Simple greedy selection: prefer diversity across subsystems
    selected = _greedy_select(valid, requested_size)

    if len(selected) < 1:
        console.print("[red]Error:[/red] No candidates could be selected.")
        db.close()
        sys.exit(1)

    # ── Create benchmark ───────────────────────────────────────────────────
    owner_repo = get_github_owner_repo(git_root)
    repository_remote = f"github.com/{owner_repo[0]}/{owner_repo[1]}" if owner_repo else ""

    # Convert selected dicts to CandidateTask objects for health calculation
    cand_tasks = [CandidateTask(**s) for s in selected]
    health = calculate_health(cand_tasks)

    benchmark_id = f"rb_b_{datetime.now(UTC).strftime('%Y%m%d')}_{uuid.uuid4().hex[:4]}"

    task_ids = []
    task_rows = []
    for i, cand in enumerate(selected, start=1):
        task_id = f"rb_t_{i:03d}"
        task_ids.append(task_id)
        task_rows.append((benchmark_id, task_id, cand["candidate_id"]))

    manifest = BenchmarkManifest(
        benchmark_id=benchmark_id,
        repository_remote=repository_remote,
        repository_private=True,
        workload_window_days=config.repository.lookback_days,
        workload_window_prs=db.count_prs(),
        tasks=task_ids,
        health=health,
    )

    db.upsert_benchmark(
        {
            "benchmark_id": benchmark_id,
            "repository_remote": repository_remote,
            "repository_private": True,
            "created_at": manifest.created_at,
            "workload_window_days": config.repository.lookback_days,
            "workload_window_prs": db.count_prs(),
            "health": health.model_dump(),
            "coverage_warnings": manifest.coverage_warnings,
            "benchmark_json": manifest.model_dump_json(),
        }
    )

    for benchmark_id_row, task_id, candidate_id in task_rows:
        db.upsert_benchmark_task(benchmark_id_row, task_id, candidate_id)

    # ── Write manifest file ────────────────────────────────────────────────
    benchmark_dir = repobench_dir / "benchmarks" / benchmark_id
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = benchmark_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))

    # ── Print summary ──────────────────────────────────────────────────────
    table = Table(title=f"Benchmark Created: {benchmark_id}", show_header=True, header_style="bold")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Tasks", str(len(selected)))
    table.add_row("Health", str(health.overall))
    table.add_row("Representativeness", str(health.representativeness))
    table.add_row("Validation", str(health.validation))
    table.add_row("Leakage", str(health.leakage))
    table.add_row("Recency", str(health.recency))
    table.add_row("Diversity", str(health.diversity))
    table.add_row("Valid candidates", str(len(valid)))
    table.add_row("Requested size", str(requested_size))

    console.print(table)

    if len(selected) < requested_size:
        console.print(
            f"[yellow]Note:[/yellow] Requested {requested_size} tasks but only "
            f"{len(valid)} valid candidates available. Used {len(selected)}."
        )

    console.print(
        "\n[bold]Next steps:[/bold]\n"
        "  repobench run <config> <config> --benchmark " + benchmark_id + "\n"
        "  repobench report --benchmark " + benchmark_id
    )

    db.close()


def _greedy_select(candidates: list[dict], size: int) -> list[dict]:
    """Greedy selection preferring subsystem diversity."""
    size = min(size, len(candidates))
    if size <= 0:
        return []

    selected: list[dict] = []
    subsystem_pool: dict[str, list[dict]] = {}
    for cand in candidates:
        subsystem_pool.setdefault(cand.get("subsystem", "unknown"), []).append(cand)

    # Round-robin across subsystems first
    subsystems = list(subsystem_pool.keys())
    idx = 0
    while len(selected) < size and subsystems:
        subsystem = subsystems[idx % len(subsystems)]
        pool = subsystem_pool[subsystem]
        if pool:
            selected.append(pool.pop(0))
        else:
            subsystems.remove(subsystem)
            continue
        idx += 1

    # Fill remaining slots with leftover candidates
    remaining = [c for c in candidates if c not in selected]
    for cand in remaining:
        if len(selected) >= size:
            break
        selected.append(cand)

    return selected[:size]


def run_benchmark_list() -> None:
    """List existing benchmarks."""
    setup_logging()
    db, repobench_dir = get_database()

    # Query benchmarks from manifest files (more reliable)
    benchmarks_dir = repobench_dir / "benchmarks"
    manifests = sorted(benchmarks_dir.glob("*/manifest.json")) if benchmarks_dir.exists() else []

    if not manifests:
        console.print(
            "[yellow]No benchmarks found.[/yellow]\n"
            "Run [bold]repobench benchmark build[/bold] to create one."
        )
        db.close()
        return

    table = Table(title="Benchmarks", show_header=True, header_style="bold")
    table.add_column("Benchmark ID", style="cyan")
    table.add_column("Created", style="green")
    table.add_column("Tasks", justify="right")
    table.add_column("Health", justify="right")
    table.add_column("Manifest", style="dim")

    for mf in manifests:
        try:
            data = json.loads(mf.read_text())
        except (ValueError, OSError):
            continue
        table.add_row(
            data.get("benchmark_id", "?"),
            str(data.get("created_at", "?")),
            str(len(data.get("tasks", []))),
            str(data.get("health", {}).get("overall", "?")),
            str(mf.relative_to(repobench_dir)),
        )

    console.print(table)
    console.print("\n[dim]Inspect:[/dim] [bold]repobench benchmark show <id>[/bold]")

    db.close()


def run_benchmark_show(benchmark_id: str) -> None:
    """Show benchmark details and health."""
    setup_logging()
    db, repobench_dir = get_database()

    manifest_path = repobench_dir / "benchmarks" / benchmark_id / "manifest.json"
    if not manifest_path.exists():
        console.print(f"[red]Error:[/red] Benchmark not found: {benchmark_id}")
        db.close()
        sys.exit(1)

    data = json.loads(manifest_path.read_text())

    console.print(f"[bold cyan]Benchmark {benchmark_id}[/bold cyan]\n")

    info = Table(show_header=False, box=None)
    info.add_column("Field", style="bold", min_width=22)
    info.add_column("Value")

    info.add_row("Repository", data.get("repository_remote", "unknown"))
    info.add_row("Created", str(data.get("created_at", "")))
    info.add_row(
        "Workload window",
        f"{data.get('workload_window_days', '?')} days / "
        f"{data.get('workload_window_prs', '?')} PRs",
    )
    info.add_row("Tasks", str(len(data.get("tasks", []))))
    console.print(info)

    # Health table
    health = data.get("health", {})
    health_table = Table(title="Benchmark Health", show_header=True, header_style="bold")
    health_table.add_column("Component", style="bold")
    health_table.add_column("Score", justify="right")
    for key in ("overall", "representativeness", "validation", "leakage", "recency", "diversity"):
        health_table.add_row(key.capitalize(), str(health.get(key, 0)))
    console.print(health_table)

    # Task list
    tasks = data.get("tasks", [])
    if tasks:
        task_table = Table(title="Tasks", show_header=True, header_style="bold")
        task_table.add_column("Task ID", style="cyan")
        task_table.add_column("Candidate", style="magenta")
        task_table.add_column("Type", style="green")
        task_table.add_column("Subsystem", style="yellow")

        for task_id in tasks:
            bt = db.conn.execute(
                "SELECT candidate_id FROM benchmark_tasks WHERE benchmark_id = ? AND task_id = ?",
                (benchmark_id, task_id),
            ).fetchone()
            cand_id = bt["candidate_id"] if bt else "?"
            cand = db.get_candidate(cand_id) if cand_id != "?" else None
            task_table.add_row(
                task_id,
                cand_id,
                (cand or {}).get("task_type", "?"),
                (cand or {}).get("subsystem", "?"),
            )
        console.print(task_table)

    # Warnings
    warnings = data.get("coverage_warnings", [])
    if warnings:
        console.print(
            Panel(
                "\n".join(f"⚠ {w}" for w in warnings),
                title="Coverage Warnings",
                border_style="yellow",
            )
        )

    db.close()
