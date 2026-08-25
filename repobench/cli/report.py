"""RepoBench Report — generate comparison reports from benchmark results."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repobench.analysis.metrics import compute_config_metrics
from repobench.analysis.statistics import paired_bootstrap_difference
from repobench.cli.utils import get_database
from repobench.logging import setup_logging
from repobench.models import BenchmarkHealth, ComparisonPair, Report
from repobench.storage.database import Database

console = Console()


def _build_report(db: Database, benchmark_id: str) -> Report:
    """Build a Report from benchmark trials."""
    trials = db.get_trials_for_benchmark(benchmark_id)

    # Group trials by agent config
    by_config: dict[str, list[dict]] = {}
    for t in trials:
        by_config.setdefault(t.get("agent_config", "?"), []).append(t)

    config_metrics = {name: compute_config_metrics(ts) for name, ts in by_config.items()}

    # Paired comparisons between configurations
    comparisons: list[ComparisonPair] = []
    config_names = list(config_metrics.keys())
    for i in range(len(config_names)):
        for j in range(i + 1, len(config_names)):
            a, b = config_names[i], config_names[j]
            # Pair trials by task_id
            task_map_a = {t.get("task_id"): t for t in by_config[a]}
            task_map_b = {t.get("task_id"): t for t in by_config[b]}
            common_tasks = sorted(set(task_map_a) & set(task_map_b))

            outcomes_a = [1 if task_map_a[t].get("solved") else 0 for t in common_tasks]
            outcomes_b = [1 if task_map_b[t].get("solved") else 0 for t in common_tasks]

            mean_diff, ci_low, ci_high = paired_bootstrap_difference(outcomes_a, outcomes_b)
            conclusive = not (ci_low < 0 < ci_high)
            comparisons.append(
                ComparisonPair(
                    config_a=a,
                    config_b=b,
                    difference_pp=round(mean_diff, 1),
                    ci_lower_pp=round(ci_low, 1),
                    ci_upper_pp=round(ci_high, 1),
                    conclusive=conclusive,
                )
            )

    # Load benchmark manifest for health
    # (in-memory defaults; manifest loading done by caller for terminal output)
    health = None

    return Report(
        benchmark_id=benchmark_id,
        repository="",
        tasks=len(set(t.get("task_id") for t in trials)),
        health=health or BenchmarkHealth(),
        config_metrics=config_metrics,
        comparisons=comparisons,
    )


def _find_latest_benchmark(db: Database, repobench_dir: Path) -> str:
    benchmarks_dir = repobench_dir / "benchmarks"
    manifests = sorted(benchmarks_dir.glob("*/manifest.json")) if benchmarks_dir.exists() else []
    if not manifests:
        console.print(
            "[red]Error:[/red] No benchmarks found. "
            "Run [bold]repobench benchmark build[/bold] first."
        )
        sys.exit(1)
    data = json.loads(manifests[-1].read_text())
    return data.get("benchmark_id", manifests[-1].parent.name)


def run_report(benchmark_id: str | None, fmt: str, output: str | None) -> None:
    """Generate comparison report from benchmark results."""
    setup_logging()
    db, repobench_dir = get_database()

    if fmt not in ("terminal", "json", "html"):
        console.print(
            f"[red]Error:[/red] Unknown format: {fmt}\n"
            "Supported: terminal, json, html (html is P1, not yet implemented)."
        )
        db.close()
        sys.exit(1)

    resolved_id = benchmark_id or _find_latest_benchmark(db, repobench_dir)

    # Load manifest for health
    manifest_path = repobench_dir / "benchmarks" / resolved_id / "manifest.json"
    manifest_data = {}
    if manifest_path.exists():
        manifest_data = json.loads(manifest_path.read_text())

    report = _build_report(db, resolved_id)

    # Attach health and repo info from manifest
    if manifest_data:
        health = manifest_data.get("health", {})
        report.health = BenchmarkHealth(
            overall=health.get("overall", 0),
            representativeness=health.get("representativeness", 0),
            validation=health.get("validation", 0),
            leakage=health.get("leakage", 0),
            recency=health.get("recency", 0),
            diversity=health.get("diversity", 0),
        )
        report.repository = manifest_data.get("repository_remote", "")
        report.tasks = len(manifest_data.get("tasks", []))
        warnings = manifest_data.get("coverage_warnings", [])
        if warnings:
            report.warnings.extend(warnings)

    if fmt == "json":
        _output_json(report, output)
    elif fmt == "html":
        console.print(
            "[yellow]HTML report is P1 and not yet implemented.[/yellow]\n"
            "Use --format terminal or --format json."
        )
        db.close()
        sys.exit(1)
    else:
        _output_terminal(report)

    db.close()


def _output_terminal(report: Report) -> None:
    """Print the terminal report."""
    console.print("[bold]AGENTFIT REPORT[/bold]")
    console.print("─" * 60)
    console.print()

    info = Table(show_header=False, box=None)
    info.add_column("Field", style="bold", min_width=16)
    info.add_column("Value")
    info.add_row("Repository", report.repository or "unknown")
    info.add_row("Benchmark", report.benchmark_id)
    info.add_row("Tasks", str(report.tasks))
    info.add_row("Health", f"{report.health.overall} / 100")
    console.print(info)
    console.print()

    # Per-config metrics
    if not report.config_metrics:
        console.print(
            "[yellow]No trial data available yet.[/yellow]\n"
            "Run [bold]repobench run <config> <config>[/bold] first."
        )
        return

    table = Table(title="Configuration Results", show_header=True, header_style="bold")
    table.add_column("Config", style="cyan")
    table.add_column("Solve", justify="right")
    table.add_column("$ / Solve", justify="right")
    table.add_column("Total Cost", justify="right")
    table.add_column("p50 (ms)", justify="right")

    for name, m in report.config_metrics.items():
        solve_str = f"{m.pass_rate * 100:.0f}% ({m.solved}/{m.total})"
        cost_str = f"${m.cost_per_solve:.2f}" if m.cost_per_solve is not None else "—"
        table.add_row(
            name,
            solve_str,
            cost_str,
            f"${m.total_cost:.2f}",
            str(m.p50_duration_ms) if m.p50_duration_ms is not None else "—",
        )
    console.print(table)

    # Comparisons
    if report.comparisons:
        console.print()
        for comp in report.comparisons:
            console.print(
                f"[bold]{comp.config_a}[/bold] vs [bold]{comp.config_b}[/bold]\n"
                f"  Difference: {comp.difference_pp:+.1f}pp\n"
                f"  95% CI: [{comp.ci_lower_pp:+.1f}pp, {comp.ci_upper_pp:+.1f}pp]"
            )
            if comp.conclusive:
                console.print("  [green]Conclusive difference.[/green]")
            else:
                console.print("  [yellow]No conclusive quality difference.[/yellow]")
            console.print()

    # Warnings
    if report.public_repo_warning:
        console.print(
            Panel(
                "[red]⚠ PUBLIC REPOSITORY[/red]\nModels may have seen this code during training.",
                border_style="red",
            )
        )
    for w in report.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")


def _output_json(report: Report, output: str | None) -> None:
    """Emit the report as JSON."""
    data = report.model_dump(mode="json")
    text = json.dumps(data, indent=2)

    if output:
        Path(output).write_text(text + "\n")
        console.print(f"Report written to {output}")
    else:
        console.print(text)
