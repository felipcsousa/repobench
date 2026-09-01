"""RepoBench CLI (PRD §90). Command bodies are wired to repobench.cli.services."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

from repobench import __version__

app = typer.Typer(
    name="repobench",
    help="Repository-native evals for coding agents, with simple local execution.",
    no_args_is_help=True,
)
benchmark_app = typer.Typer(help="Benchmark construction and inspection.", no_args_is_help=True)
targets_app = typer.Typer(help="Execution target management.", no_args_is_help=True)
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(targets_app, name="targets")


class CliContext:
    def __init__(self, config_path: Path | None, verbose: bool):
        self.config_path = config_path
        self.verbose = verbose


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"repobench {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
    config: Path = typer.Option(
        Path("repobench.yml"), "--config", help="Path to repobench.yml."
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Verbose logging to stderr."),
) -> None:
    from repobench.core.logging import setup_logging

    setup_logging(verbose)
    ctx.obj = CliContext(config_path=config, verbose=verbose)


# ------------------------------------------------------------------- helpers


def _fail(message: str) -> None:
    from repobench.cli.render import fail

    fail(message)
    raise typer.Exit(code=1)


def _service_context(ctx: typer.Context):
    """(root, paths, config, storage) for repo-bound commands (PRD §114-115)."""
    from repobench.cli.services import load_config, project_paths, resolve_repo_root
    from repobench.config import CONFIG_FILENAME
    from repobench.storage.db import Storage

    cctx: CliContext = ctx.obj
    root = resolve_repo_root()
    paths = project_paths(root)
    explicit = (
        cctx.config_path
        if cctx.config_path and cctx.config_path.name != CONFIG_FILENAME
        else None
    )
    cfg = load_config(root, explicit)
    return root, paths, cfg, Storage(paths.state_db)


def _short(output: str | None) -> str | None:
    first = (output or "").strip().splitlines()
    return first[0][:80] if first else None


# -------------------------------------------------------------------- doctor


def _check(label: str, ok: bool, detail: str) -> None:
    from repobench.cli.render import MISS, OK, echo

    echo(f"  {OK if ok else MISS} {label:<24}{detail}")


def _doctor_repo(root: Path | None) -> str | None:
    from repobench.cli.render import echo
    from repobench.core.gitutil import git_run
    from repobench.repository.git import slug_from_url

    slug: str | None = None
    if root is None:
        echo("  ✗ Git repository         not inside a git repository")
        return None
    _check("Git repository", True, str(root))
    result = git_run(root, "remote", "get-url", "origin")
    if result.exit_code == 0:
        slug = slug_from_url(result.stdout.strip())
        _check("GitHub remote", slug is not None, slug or "origin is not a GitHub remote")
    else:
        _check("GitHub remote", False, "no origin remote configured")
    gh_path = shutil.which("gh")
    if gh_path is None:
        _check("GitHub CLI (gh)", False, "not installed (optional; enables PR enrichment)")
    else:
        from repobench.execution.process import run_sync

        version = run_sync(["gh", "--version"], cwd=Path.cwd(), timeout_seconds=15)
        _check("GitHub CLI (gh)", True, _short(version.stdout) or gh_path)
    return slug


def _doctor_project(root: Path | None) -> None:
    from repobench.cli.render import OK, echo
    from repobench.config import detect_project_environment

    language_names = {
        "python": "Python",
        "javascript-typescript": "JavaScript/TypeScript",
    }
    if root is None:
        echo("  (not inside a repository — project detection skipped)")
        return
    project = detect_project_environment(root)
    if project.language is None:
        echo("  (no Python or Node project markers detected)")
        return
    name = language_names.get(project.language, project.language)
    pieces = [f"{OK} {name}"]
    if project.package_manager:
        pieces.append(project.package_manager)
    echo("  " + " · ".join(pieces))
    if project.install_command:
        echo(f"      install: {project.install_command}")
    if project.test_command:
        echo(f"      test:    {project.test_command}")


def _doctor_harnesses(capability_table: bool) -> None:
    from repobench.cli.render import (
        MISS,
        echo,
        render_capability_row,
    )
    from repobench.execution.adapters.registry import all_adapters

    adapters = all_adapters()
    if capability_table:
        echo("")
        echo("Capabilities (PRD §120; '?' fields are never guessed by adapters)")
        echo("")
        header = f"{'Harness':<12}{'MODEL':^7}{'JSON':^7}{'TOKENS':^7}{'COST':^7}{'PROVIDER':^10}"
        echo(header)
        for adapter in adapters.values():
            echo(render_capability_row(adapter.name, adapter.capabilities))
    echo("")
    echo("Harnesses")
    echo("")
    installed = 0
    for adapter in adapters.values():
        detection = adapter.detect()
        if adapter.name == "command":
            echo(f"  ✓ {'command':<12}generic command targets (configured per target)")
            continue
        if not detection.installed:
            echo(f"  {MISS} {adapter.name:<12}not installed")
            continue
        installed += 1
        version = detection.version or "unknown version"
        echo(f"  ✓ {adapter.name:<12}{version}")
    plural = "harness" if installed == 1 else "harnesses"
    echo("")
    echo(f"{installed} execution {plural} available.")
    echo("Auth is never probed by doctor — no inference is run (PRD §92).")


@app.command()
def doctor(
    harnesses: bool = typer.Option(
        False, "--harnesses", help="Include the per-harness capability table."
    ),
) -> None:
    """Check repository, project tooling and installed harnesses (PRD §91-92)."""
    from repobench.cli.render import echo

    echo("RepoBench Doctor")
    echo("")
    echo("Repository")
    root = None
    try:
        from repobench.core.paths import find_repo_root

        root = find_repo_root(Path.cwd())
    except Exception:
        root = None
    _doctor_repo(root)
    echo("")
    echo("Project")
    _doctor_project(root)
    _doctor_harnesses(harnesses)


# ---------------------------------------------------------------------- init


@app.command()
def init(
    yes: bool = typer.Option(False, "--yes", help="Accept suggestions without prompting."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing repobench.yml."),
) -> None:
    """Detect the project environment and write repobench.yml (PRD §95)."""
    from repobench.cli import render
    from repobench.cli.services import init_project, resolve_repo_root
    from repobench.core.errors import RepoBenchError

    try:
        root = resolve_repo_root()
        overwrite = force
        if not overwrite and (root / "repobench.yml").is_file():
            if yes:
                overwrite = False  # --yes never silently clobbers; --force is explicit
            elif sys.stdin.isatty():
                overwrite = typer.confirm(
                    "repobench.yml already exists. Overwrite it?", default=False
                )
            if not overwrite:
                render.fail(
                    "repobench.yml already exists — use --force to overwrite it"
                )
                raise typer.Exit(code=1)
        outcome = init_project(root, overwrite=overwrite)
    except RepoBenchError as exc:
        _fail(str(exc))
        return

    render.echo("RepoBench initialized")
    render.echo("")
    if outcome.created:
        render.kv("Config written", str(outcome.config_path))
    else:
        render.kv("Config overwritten", str(outcome.config_path))
    render.kv(
        ".gitignore",
        (".repobench/ appended" if outcome.gitignore_updated else "already ignores .repobench/"),
    )
    render.echo("")
    render.echo("Detected project commands (edit repobench.yml to adjust):")
    for line in render.config_summary_lines(outcome.config, outcome.root):
        render.echo(f"  {line}")
    render.echo("")
    render.echo("Next: repobench analyze")


# -------------------------------------------------------------------- analyze


@app.command()
def analyze(ctx: typer.Context) -> None:
    """Mine the Workload Universe and eval candidates from engineering history (PRD §10, §65-66)."""
    from repobench.cli import render
    from repobench.cli.services import (
        analyze_repository,
        persist_candidates,
    )
    from repobench.core.errors import RepoBenchError

    try:
        root, _paths, cfg, storage = _service_context(ctx)
        outcome = analyze_repository(root, cfg)
        persist_candidates(storage, outcome.candidates)
    except RepoBenchError as exc:
        _fail(str(exc))
        return

    lookback_note = (
        f"Enrichment: {outcome.enrichment}"
        + (f" ({outcome.remote_slug})" if outcome.enrichment == "github" else "")
        + f" · lookback {cfg.repository.lookback_days} days"
    )
    render.render_analyze_summary(outcome, outcome.summary.suggested_benchmark_size, lookback_note)
    render.echo("Next: repobench benchmark build")


@app.command()
def candidates(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(
        None, help="Filter by status (DISCOVERED / FILTERED / VALID / REJECTED)."
    ),
) -> None:
    """List mined candidates with classification and rejection codes."""
    from repobench.cli import render
    from repobench.core.errors import RepoBenchError
    from repobench.storage.db import Storage

    try:
        root, paths, _cfg, _storage = _service_context(ctx)
    except RepoBenchError as exc:
        _fail(str(exc))
        return
    storage = Storage(paths.state_db)
    rows = storage.list_candidates(status)
    if not rows:
        render.echo(
            "no candidates recorded yet — run `repobench analyze` first"
            + (f" (status {status})" if status else "")
        )
        return
    render.render_candidates_table(rows)


# ----------------------------------------------------------------- benchmark


@benchmark_app.command("build")
def benchmark_build(
    ctx: typer.Context,
    size: Optional[int] = typer.Option(None, help="Benchmark size override."),
) -> None:
    """Validate tasks and sample a representative benchmark (PRD §88-89)."""
    from repobench.cli import render
    from repobench.cli.services import build_benchmark
    from repobench.core.errors import RepoBenchError

    if size is not None and size < 1:
        _fail("--size must be at least 1")
        return
    try:
        root, _paths, cfg, storage = _service_context(ctx)
        outcome = build_benchmark(
            root, cfg, storage, size=size, log=render.render_task_build_line
        )
    except RepoBenchError as exc:
        _fail(str(exc))
        return
    render.echo("Validated task candidates:")
    render.render_benchmark_build(outcome)
    render.echo("")
    render.echo("Next: repobench run --all")


# ------------------------------------------------------------------- targets


@targets_app.command("list")
def targets_list(ctx: typer.Context) -> None:
    """List configured execution targets (PRD §93)."""
    from repobench.cli import render
    from repobench.core.errors import RepoBenchError

    try:
        root, _paths, cfg, _storage = _service_context(ctx)
    except RepoBenchError as exc:
        _fail(str(exc))
        return
    if not cfg.targets:
        render.echo("no targets configured — add them to repobench.yml")
        return
    render.render_targets_table(cfg)


@targets_app.command("validate")
def targets_validate(
    ctx: typer.Context,
    target_id: str = typer.Argument(..., help="Target id to validate."),
) -> None:
    """Structurally validate one target — no inference is performed (PRD §94)."""
    from repobench.cli import render
    from repobench.cli.services import resolve_targets, validate_targets
    from repobench.core.errors import RepoBenchError

    targets = []
    try:
        root, _paths, cfg, _storage = _service_context(ctx)
        targets = resolve_targets(cfg, [target_id], all_targets=False)
        validate_targets(targets)
    except RepoBenchError as exc:
        _fail(str(exc))
        return
    target = targets[0]
    render.echo(f"✓ target {target.id} (harness: {target.harness}) is structurally valid")


# ----------------------------------------------------------------------- run


@app.command()
def run(
    ctx: typer.Context,
    target_ids: list[str] = typer.Argument(None, help="Target ids to run."),
    all_targets: bool = typer.Option(False, "--all", help="Run all configured targets."),
    benchmark: Optional[str] = typer.Option(None, help="Benchmark id (default: latest)."),
    jobs: Optional[int] = typer.Option(None, "--jobs", help="Concurrent trials."),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive mode for scripts."),
    resume: bool = typer.Option(
        False, "--resume", help="Resume pending / infrastructure-failed trials of the latest run."
    ),
    keep_workspaces: bool = typer.Option(
        False, "--keep-workspaces", help="Keep trial workspaces under .repobench/workspaces/."
    ),
    rollouts: int = typer.Option(1, help="Trials per Task×Target (V1 supports 1; V1.5 adds more)."),
) -> None:
    """Execute Benchmark × Targets locally (PRD §96-99)."""
    from repobench.cli import render
    from repobench.cli.services import (
        execute_plan,
        plan_run,
        resolve_targets,
        validate_targets,
    )
    from repobench.core.errors import RepoBenchError

    if rollouts != 1:
        _fail("multiple rollouts arrive in V1.5 (PRD §103); V1 supports exactly 1 rollout")
        return

    try:
        root, paths, cfg, storage = _service_context(ctx)
        targets = resolve_targets(cfg, list(target_ids or []), all_targets)
        validate_targets(targets)
        plan = plan_run(
            storage,
            paths,
            cfg,
            targets=targets,
            benchmark_id=benchmark,
            resume=resume,
            jobs=jobs,
            keep=True if keep_workspaces else None,
        )
    except RepoBenchError as exc:
        _fail(str(exc))
        return

    note = "resuming the latest run" if plan.is_resume else "creating a new run"
    render.echo("Running benchmark")
    render.render_run_preview(plan, note)
    if not yes:
        typer.confirm("Continue?", abort=True)

    try:
        outcome = execute_plan(
            root,
            cfg,
            storage,
            plan,
            progress=render.render_progress,
        )
    except RepoBenchError as exc:
        _fail(str(exc))
        return
    render.render_run_summary(outcome.results)
    render.echo("")
    render.echo(f"Run {plan.run_id} completed. Compare targets with `repobench report`.")


# --------------------------------------------------------------------- report


@app.command()
def report(
    ctx: typer.Context,
    run_id: Optional[str] = typer.Option(None, help="Run id (default: latest)."),
    format: str = typer.Option("text", "--format", help="text | json (html is P1)."),
) -> None:
    """Show the comparison report for a run (PRD §111-112)."""
    from repobench.cli import render
    from repobench.cli.services import build_report_data
    from repobench.core.errors import RepoBenchError
    from repobench.reporting.json_report import render_json
    from repobench.reporting.terminal import render_report

    if format == "html":
        render.echo("HTML report is P1 (PRD §113)")
        return
    if format not in ("text", "json"):
        _fail(f"unknown report format: {format!r} (expected text | json)")
        return

    try:
        root, _paths, cfg, storage = _service_context(ctx)
        data = build_report_data(root, cfg, storage, run_id=run_id)
    except RepoBenchError as exc:
        _fail(str(exc))
        return

    if format == "json":
        typer.echo(render_json(data))
    else:
        render.echo(render_report(data))


def run_cli() -> None:
    app()


if __name__ == "__main__":
    run_cli()
