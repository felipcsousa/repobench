"""Terminal report rendering with Rich."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repobench.logging import get_logger
from repobench.models import ConfigMetrics, Report

log = get_logger("reporting.terminal")


def print_report(report: Report, console: Console | None = None) -> None:
    """Print the full RepoBench report to the terminal."""
    console = console or Console()

    console.print()
    console.print("[bold cyan]AGENTFIT REPORT[/bold cyan]")
    console.print("[dim]────────────────────────────────────────[/dim]")
    console.print()

    # ── Header info ──
    console.print(f"[bold]Repository[/bold]  {report.repository}")
    console.print(f"[bold]Benchmark[/bold]   {report.benchmark_id}")
    console.print(f"[bold]Tasks[/bold]       {report.tasks}")
    console.print()

    # ── Health ──
    h = report.health
    console.print(
        Panel(
            f"Benchmark Health: [bold]{h.overall} / 100[/bold]\n\n"
            f"  Representativeness  [green]{h.representativeness}[/green]\n"
            f"  Validation          [green]{h.validation}[/green]\n"
            f"  Leakage             [yellow]{h.leakage}[/yellow]\n"
            f"  Recency             [green]{h.recency}[/green]\n"
            f"  Diversity           [green]{h.diversity}[/green]",
            title="Health",
            border_style="cyan",
        )
    )
    console.print()

    # ── Config results table ──
    if report.config_metrics:
        table = Table(title="Configuration Results", border_style="blue")
        table.add_column("Config", style="bold")
        table.add_column("Solve", justify="right")
        table.add_column("$/Solve", justify="right")
        table.add_column("Total Cost", justify="right")
        table.add_column("p50", justify="right")

        for name, m in sorted(
            report.config_metrics.items(), key=lambda kv: kv[1].pass_rate, reverse=True
        ):
            solve_str = f"{m.pass_rate * 100:.0f}% ({m.solved}/{m.total})"
            cost_str = f"${m.cost_per_solve:.2f}" if m.cost_per_solve is not None else "—"
            total_cost = f"${m.total_cost:.2f}" if m.total_cost else "—"
            p50 = f"{m.p50_duration_ms}ms" if m.p50_duration_ms else "—"
            table.add_row(name, solve_str, cost_str, total_cost, p50)

        console.print(table)
        console.print()

    # ── Comparisons ──
    for comp in report.comparisons:
        diff_str = (
            f"{comp.difference_pp:+.0f}pp"
            if comp.difference_pp >= 0
            else f"{comp.difference_pp:.0f}pp"
        )
        console.print(f"[bold]{comp.config_a}[/bold] {diff_str} vs [bold]{comp.config_b}[/bold]")
        console.print(f"  95% CI: [{comp.ci_lower_pp:+.0f}pp, {comp.ci_upper_pp:+.0f}pp]")
        if comp.conclusive:
            console.print("  [green]Conclusive quality difference.[/green]")
        else:
            console.print("  [yellow]No conclusive quality difference.[/yellow]")
        console.print()

    # ── Recommendation ──
    if report.recommendation:
        console.print(
            Panel(
                f"[bold]{report.recommendation}[/bold]\n\n{report.recommendation_reason or ''}",
                title="Recommendation",
                border_style="green",
            )
        )
        console.print()

    # ── Warnings ──
    if report.warnings:
        console.print("[bold yellow]Benchmark warnings[/bold yellow]")
        for w in report.warnings:
            console.print(f"  [yellow]⚠ {w}[/yellow]")
        console.print()

    if report.public_repo_warning:
        console.print(
            Panel(
                "[bold red]PUBLIC REPOSITORY[/bold red]\n\n"
                "Models may have been exposed to this code or historical pull "
                "requests during training.\n\n"
                "Benchmark measures practical performance, "
                "not contamination-free capability.",
                border_style="red",
            )
        )
        console.print()


def print_pareto(metrics_dict: dict[str, ConfigMetrics], console: Console | None = None) -> None:
    """Print the Pareto frontier of quality vs cost."""
    console = console or Console()

    if not metrics_dict:
        return

    from repobench.analysis.recommendation import pareto_frontier

    frontier = pareto_frontier(metrics_dict)

    console.print("[bold]Pareto Frontier (quality vs cost)[/bold]")
    console.print()

    rows = []
    for name, m in sorted(metrics_dict.items(), key=lambda kv: kv[1].pass_rate, reverse=True):
        marker = "●" if name in frontier else "○"
        cost = f"${m.cost_per_solve:.2f}/solve" if m.cost_per_solve is not None else "n/a"
        rows.append((marker, name, f"{m.pass_rate * 100:.0f}%", cost))

    if rows:
        table = Table(border_style="blue")
        table.add_column("", width=2)
        table.add_column("Config", style="bold")
        table.add_column("Quality", justify="right")
        table.add_column("Cost", justify="right")
        for marker, name, quality, cost in rows:
            table.add_row(marker, name, quality, cost)
        console.print(table)

    console.print("[dim]● = on Pareto frontier[/dim]")
    console.print()


def print_segments(segments, console: Console | None = None) -> None:
    """Print segment analysis table."""
    console = console or Console()

    if not segments:
        return

    console.print("[bold]Segment Analysis[/bold]")
    console.print()

    # Collect all config names
    config_names: set[str] = set()
    for seg in segments:
        config_names.update(seg.metrics.keys())

    table = Table(border_style="blue")
    table.add_column("Category", style="bold")
    for name in sorted(config_names):
        table.add_column(name, justify="right")

    for seg in segments:
        row = [seg.category]
        for name in sorted(config_names):
            m = seg.metrics.get(name)
            if m and m.total > 0:
                row.append(f"{m.pass_rate * 100:.0f}%")
            else:
                row.append("—")
        table.add_row(*row)

    console.print(table)
    console.print("[dim]n < 5 per segment: descriptive only[/dim]")
    console.print()
