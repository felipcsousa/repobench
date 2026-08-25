"""RepoBench Run — run agent configurations against a benchmark via Harbor."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.table import Table

from repobench.cli.utils import get_database
from repobench.config import load_config
from repobench.harbor.exporter import export_single_task
from repobench.harbor.runner import check_harbor_available, run_harbor_trial
from repobench.logging import get_logger, setup_logging
from repobench.models import CandidateTask, Trial, ValidTask, VerifierResult
from repobench.storage.database import Database
from repobench.utils import get_git_root

logger = get_logger("run")
console = Console()


def _resolve_benchmark(db: Database, repobench_dir: Path, benchmark_id: str | None) -> str:
    """Resolve benchmark ID, defaulting to the most recent."""
    if benchmark_id:
        manifest_path = repobench_dir / "benchmarks" / benchmark_id / "manifest.json"
        if not manifest_path.exists():
            console.print(f"[red]Error:[/red] Benchmark not found: {benchmark_id}")
            sys.exit(1)
        return benchmark_id

    # Find most recent benchmark
    benchmarks_dir = repobench_dir / "benchmarks"
    manifests = sorted(benchmarks_dir.glob("*/manifest.json")) if benchmarks_dir.exists() else []
    if not manifests:
        console.print(
            "[red]Error:[/red] No benchmarks found.\n"
            "Run [bold]repobench benchmark build[/bold] first."
        )
        sys.exit(1)
    latest = manifests[-1]
    data = json.loads(latest.read_text())
    return data.get("benchmark_id", latest.parent.name)


def _load_tasks_for_benchmark(db: Database, benchmark_id: str) -> list[ValidTask]:
    """Load ValidTask objects from the database for a benchmark."""
    bt_rows = db.get_benchmark_tasks(benchmark_id)
    tasks: list[ValidTask] = []

    for row in bt_rows:
        cand_data = db.get_candidate(row["candidate_id"])
        if cand_data is None:
            continue

        # Reconstruct CandidateTask from DB
        from repobench.models import (
            Complexity,
            Eligibility,
            InstructionProvenance,
            NetworkIsolation,
            TaskStatus,
            TaskType,
        )

        elig = Eligibility(
            history=bool(cand_data.get("eligibility_history")),
            instruction=bool(cand_data.get("eligibility_instruction")),
            verifier=bool(cand_data.get("eligibility_verifier")),
        )

        candidate = CandidateTask(
            candidate_id=cand_data["candidate_id"],
            pr_number=cand_data["pr_number"],
            pr_title=cand_data.get("pr_title", ""),
            base_sha=cand_data.get("base_sha", ""),
            gold_sha=cand_data.get("gold_sha", ""),
            merge_commit_sha=cand_data.get("merge_commit_sha"),
            head_commit_sha=cand_data.get("head_commit_sha"),
            task_type=TaskType(cand_data.get("task_type", "unknown")),
            task_type_confidence=cand_data.get("task_type_confidence", 0.0),
            subsystem=cand_data.get("subsystem", "unknown"),
            complexity=Complexity(cand_data.get("complexity", "medium")),
            implementation_loc=cand_data.get("implementation_loc", 0),
            implementation_files=cand_data.get("implementation_files", 0),
            test_loc=cand_data.get("test_loc", 0),
            test_files=cand_data.get("test_files", 0),
            instruction_source=cand_data.get("instruction_source", ""),
            instruction_provenance=(
                InstructionProvenance(cand_data["instruction_provenance"])
                if cand_data.get("instruction_provenance")
                else None
            ),
            instruction_text=cand_data.get("instruction_text"),
            status=TaskStatus(cand_data.get("status", "discovered")),
            eligibility=elig,
            leakage_risk=cand_data.get("leakage_risk", 0.0),
            network_isolation=NetworkIsolation(cand_data.get("network_isolation", "NONE")),
        )

        task = ValidTask(
            task_id=row["task_id"],
            candidate=candidate,
            instruction_text=cand_data.get("instruction_text") or f"Fix PR #{candidate.pr_number}",
            verifier_files=[],
            implementation_files_list=[],
        )
        tasks.append(task)

    return tasks


def run_benchmark(
    configs: list[str],
    benchmark_id: str | None = None,
    yes: bool = False,
    verbose: bool = False,
) -> None:
    """Run agent configurations against a benchmark via Harbor."""
    setup_logging(verbose=verbose)
    db, repobench_dir = get_database()
    git_root = get_git_root(Path.cwd()) or Path.cwd()

    if not configs:
        console.print("[red]Error:[/red] No agent configurations specified.")
        console.print("Usage: repobench run <config-name> [<config-name>...]")
        db.close()
        sys.exit(1)

    # ── Load agent configurations ─────────────────────────────────────────
    config = load_config(git_root)

    resolved_configs: list[dict] = []
    for name in configs:
        agent_cfg = config.agents.get(name)
        if agent_cfg is None:
            stored = db.get_agent_config(name)
            if stored is None:
                console.print(
                    f"[red]Error:[/red] Agent configuration not found: [bold]{name}[/bold]\n"
                    f"Available: {', '.join(config.agents.keys()) or '(none)'}\n"
                    "Add to repobench.yml under [bold]agents:[/bold]"
                )
                db.close()
                sys.exit(1)
            resolved_configs.append(stored)
        else:
            resolved_configs.append(
                {
                    "config_name": name,
                    "agent": agent_cfg.agent,
                    "model": agent_cfg.model,
                    "reasoning": agent_cfg.reasoning,
                }
            )

    # ── Resolve benchmark ─────────────────────────────────────────────────
    resolved_benchmark = _resolve_benchmark(db, repobench_dir, benchmark_id)
    tasks = _load_tasks_for_benchmark(db, resolved_benchmark)
    if not tasks:
        console.print(f"[red]Error:[/red] Benchmark {resolved_benchmark} has no tasks.")
        db.close()
        sys.exit(1)

    # ── Show trial matrix ─────────────────────────────────────────────────
    n_tasks = len(tasks)
    n_configs = len(resolved_configs)
    n_trials = n_tasks * n_configs

    table = Table(title="Execution Matrix", show_header=True, header_style="bold")
    table.add_column("Benchmark", style="cyan")
    table.add_column("Tasks", justify="right")
    table.add_column("Configurations", justify="right")
    table.add_column("Trials", justify="right")

    table.add_row(resolved_benchmark, str(n_tasks), str(n_configs), str(n_trials))
    console.print(table)

    cfg_table = Table(show_header=True, header_style="bold")
    cfg_table.add_column("Config", style="cyan")
    cfg_table.add_column("Agent", style="green")
    cfg_table.add_column("Model", style="magenta")

    for cfg in resolved_configs:
        cfg_table.add_row(
            cfg.get("config_name", "?"),
            cfg.get("agent", "?"),
            cfg.get("model") or "default",
        )
    console.print(cfg_table)

    # ── Confirmation ──────────────────────────────────────────────────────
    if not yes:
        proceed = Confirm.ask(f"\nProceed with {n_trials} trials?", default=False)
        if not proceed:
            console.print("[yellow]Aborted.[/yellow]")
            db.close()
            sys.exit(0)

    # ── Check Harbor availability ─────────────────────────────────────────
    harbor_ok, harbor_version = check_harbor_available()
    if not harbor_ok:
        console.print(
            Panel(
                "[yellow]Harbor not found on PATH.[/yellow]\n"
                "RepoBench will export tasks but cannot execute them.\n"
                "Install Harbor: pip install harbor-py",
                title="Warning",
                border_style="yellow",
            )
        )

    # ── Export tasks to Harbor format ─────────────────────────────────────
    harbor_dir = repobench_dir / "benchmarks" / resolved_benchmark / "harbor"
    network_mode = (
        config.execution.environment if hasattr(config.execution, "environment") else "no-network"
    )

    console.print("\n[cyan]Exporting tasks to Harbor format...[/cyan]")
    for idx, task in enumerate(tasks, start=1):
        task_dir = harbor_dir / f"task-{idx:03d}"
        export_single_task(
            resolved_benchmark,
            task,
            idx,
            task_dir,
            network_mode=network_mode,
        )
    console.print(f"  Exported {len(tasks)} tasks to {harbor_dir}")

    # ── Execute trials ────────────────────────────────────────────────────
    concurrency = config.execution.concurrency

    if harbor_ok:
        console.print(f"\n[cyan]Running trials (concurrency={concurrency})...[/cyan]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            overall_task = progress.add_task("Executing trials...", total=n_trials)
            completed = 0

            for cfg in resolved_configs:
                config_name = cfg.get("config_name", "?")
                agent_name = cfg.get("agent", "")
                model_name = cfg.get("model", "")

                # Create run record
                run_id = f"rb_run_{uuid.uuid4().hex[:12]}"
                db.insert_run(
                    {
                        "run_id": run_id,
                        "benchmark_id": resolved_benchmark,
                        "agent_config": config_name,
                        "agent_version": "",
                        "model_name": model_name,
                        "harbor_version": harbor_version,
                        "created_at": datetime.now(UTC),
                    }
                )

                for idx, task in enumerate(tasks, start=1):
                    task_dir = harbor_dir / f"task-{idx:03d}"
                    progress.update(
                        overall_task,
                        description=f"[{config_name}] Task {idx}/{n_tasks}",
                    )

                    # Run trial
                    result = run_harbor_trial(
                        task_dir=task_dir,
                        agent=agent_name,
                        model=model_name,
                        timeout=600,
                        verbose=verbose,
                    )

                    # Build trial
                    trial_id = f"rb_tr_{uuid.uuid4().hex[:12]}"
                    trial = Trial(
                        trial_id=trial_id,
                        run_id=run_id,
                        benchmark_id=resolved_benchmark,
                        task_id=task.task_id,
                        agent_config=config_name,
                        solved=result.solved,
                        duration_ms=result.duration_ms,
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        cost_usd=result.cost_usd,
                        verifier=VerifierResult(task=result.solved, regression=None),
                        error=result.error,
                    )
                    db.insert_trial(trial.model_dump())

                    completed += 1
                    progress.advance(overall_task)

        console.print(f"\n[green]Completed {completed} trials.[/green]")

    else:
        # Harbor not available — create pending trials
        console.print("\n[yellow]Creating pending trial records...[/yellow]")
        for cfg in resolved_configs:
            config_name = cfg.get("config_name", "?")
            run_id = f"rb_run_{uuid.uuid4().hex[:12]}"
            db.insert_run(
                {
                    "run_id": run_id,
                    "benchmark_id": resolved_benchmark,
                    "agent_config": config_name,
                    "agent_version": "",
                    "model_name": cfg.get("model") or "",
                    "harbor_version": "",
                    "created_at": datetime.now(UTC),
                }
            )

            for task in tasks:
                trial_id = f"rb_tr_{uuid.uuid4().hex[:12]}"
                db.insert_trial(
                    {
                        "trial_id": trial_id,
                        "run_id": run_id,
                        "benchmark_id": resolved_benchmark,
                        "task_id": task.task_id,
                        "agent_config": config_name,
                        "solved": False,
                        "duration_ms": None,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "cost_usd": None,
                        "verifier": {"task": None, "regression": None},
                        "error": "pending",
                        "created_at": datetime.now(UTC),
                    }
                )

        console.print(
            Panel(
                "[yellow]Trials recorded as pending.[/yellow]\n"
                "Install Harbor and re-run to execute.",
                border_style="yellow",
            )
        )

    console.print(
        f"\n[bold]Next steps:[/bold]\n  repobench report --benchmark {resolved_benchmark}"
    )

    db.close()
