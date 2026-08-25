"""RepoBench CLI — main entry point."""

from __future__ import annotations

import typer
from rich.console import Console

from repobench import __version__

app = typer.Typer(
    name="repobench",
    help="Living repository-native evals for coding agents.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# ── Sub-commands ───────────────────────────────────────────────────────────────


# Doctor
@app.command()
def doctor(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output"),
) -> None:
    """Check prerequisites for RepoBench."""
    from repobench.cli.doctor import run_doctor

    run_doctor(verbose=verbose)


# Init
@app.command()
def init(
    lookback_days: int = typer.Option(180, help="Days of history to analyze"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config"),
    add_gitignore: bool = typer.Option(True, help="Add .repobench/ to .gitignore"),
) -> None:
    """Initialize RepoBench in the current repository."""
    from repobench.cli.init import run_init

    run_init(lookback_days=lookback_days, force=force, add_gitignore=add_gitignore)


# Analyze
@app.command()
def analyze(
    resync: bool = typer.Option(False, "--resync", help="Force full re-sync from GitHub"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Analyze repository workload from Git and GitHub history."""
    from repobench.cli.analyze import run_analyze

    run_analyze(resync=resync, verbose=verbose)


# Candidates
@app.command()
def candidates(
    status_filter: str | None = typer.Option(None, "--status", help="Filter by status"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show details"),
) -> None:
    """Show candidate tasks mined from repository history."""
    from repobench.cli.candidates import run_candidates

    run_candidates(status_filter=status_filter, verbose=verbose)


# Task sub-app
task_app = typer.Typer(help="Inspect and validate individual tasks.")
app.add_typer(task_app, name="task")


@task_app.command("inspect")
def task_inspect(
    task_id: str = typer.Argument(help="Candidate or task ID to inspect"),
) -> None:
    """Inspect a candidate or task in detail."""
    from repobench.cli.task import run_task_inspect

    run_task_inspect(task_id)


@task_app.command("validate")
def task_validate(
    task_id: str = typer.Argument(help="Candidate ID to validate"),
    force: bool = typer.Option(False, "--force", help="Re-validate even if already validated"),
) -> None:
    """Run the full validation pipeline on a candidate task."""
    from repobench.cli.task import run_task_validate

    run_task_validate(task_id, force=force)


# Benchmark sub-app
benchmark_app = typer.Typer(help="Build and manage benchmarks.")
app.add_typer(benchmark_app, name="benchmark")


@benchmark_app.command("build")
def benchmark_build(
    size: int | None = typer.Option(None, "--size", help="Number of tasks"),
    force: bool = typer.Option(False, "--force", help="Rebuild existing benchmark"),
) -> None:
    """Build a representative benchmark from valid candidates."""
    from repobench.cli.benchmark import run_benchmark_build

    run_benchmark_build(size=size, force=force)


@benchmark_app.command("list")
def benchmark_list() -> None:
    """List existing benchmarks."""
    from repobench.cli.benchmark import run_benchmark_list

    run_benchmark_list()


@benchmark_app.command("show")
def benchmark_show(
    benchmark_id: str = typer.Argument(help="Benchmark ID"),
) -> None:
    """Show benchmark details and health."""
    from repobench.cli.benchmark import run_benchmark_show

    run_benchmark_show(benchmark_id)


# Run
@app.command()
def run(
    configs: list[str] = typer.Argument(help="Agent configuration names to run"),
    benchmark_id: str | None = typer.Option(None, "--benchmark", "-b", help="Benchmark ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Run agent configurations against a benchmark via Harbor."""
    from repobench.cli.run import run_benchmark

    run_benchmark(configs=configs, benchmark_id=benchmark_id, yes=yes, verbose=verbose)


# Report
@app.command()
def report(
    benchmark_id: str | None = typer.Option(None, "--benchmark", "-b", help="Benchmark ID"),
    format: str = typer.Option(
        "terminal",
        "--format",
        "-f",
        help="Output format: terminal, json, html",
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Output file path"),
) -> None:
    """Generate comparison report from benchmark results."""
    from repobench.cli.report import run_report

    run_report(benchmark_id=benchmark_id, fmt=format, output=output)


# Config sub-app
config_app = typer.Typer(help="Manage RepoBench configuration.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Show current configuration."""
    from repobench.cli.config_cmd import run_config_show

    run_config_show()


@config_app.command("set")
def config_set(
    key: str = typer.Argument(help="Configuration key (e.g., benchmark.size)"),
    value: str = typer.Argument(help="Value to set"),
) -> None:
    """Set a configuration value."""
    from repobench.cli.config_cmd import run_config_set

    run_config_set(key=key, value=value)


# Telemetry
@app.command()
def telemetry(
    action: str = typer.Argument(help="enable, disable, or status"),
) -> None:
    """Manage anonymous telemetry."""
    from repobench.cli.telemetry import run_telemetry

    run_telemetry(action=action)


def version_callback(value: bool) -> None:
    if value:
        console.print(f"Repobench v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    """RepoBench — Living repository-native evals for coding agents."""


if __name__ == "__main__":
    app()
