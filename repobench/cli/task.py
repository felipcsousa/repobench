"""RepoBench Task — inspect and validate individual candidate tasks."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repobench.cli.utils import get_database
from repobench.logging import setup_logging

console = Console()


def run_task_inspect(task_id: str) -> None:
    """Inspect a candidate or task in detail."""
    setup_logging()
    db, repobench_dir = get_database()

    # Try candidates table first
    candidate = db.get_candidate(task_id)
    if candidate is None:
        # Resolve task ID (rb_t_...) via benchmark_tasks -> candidate_id
        row = db.conn.execute(
            "SELECT candidate_id FROM benchmark_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row:
            candidate = db.get_candidate(row["candidate_id"])
    if candidate is None:
        # Try as task ID: look up by candidate_id across all candidates
        candidates = db.get_candidates_by_status("valid")
        task_match = None
        for cand in candidates:
            cand_id = cand.get("candidate_id", "")
            if cand_id == task_id:
                task_match = cand
                break
        if task_match:
            candidate = task_match
        else:
            console.print(f"[red]Error:[/red] Candidate/task not found: {task_id}")
            db.close()
            sys.exit(1)

    # ── Build inspection panel ─────────────────────────────────────────────
    console.print(f"[bold cyan]Candidate {candidate.get('candidate_id', '?')}[/bold cyan]\n")

    info = Table(show_header=False, box=None)
    info.add_column("Field", style="bold", min_width=22)
    info.add_column("Value")

    info.add_row("PR", f"#{candidate.get('pr_number', '?')} — {candidate.get('pr_title', '')}")
    info.add_row(
        "Type",
        f"{candidate.get('task_type', 'unknown')} "
        f"(conf {candidate.get('task_type_confidence', 0):.2f})",
    )
    info.add_row("Subsystem", candidate.get("subsystem", "unknown"))
    info.add_row("Complexity", candidate.get("complexity", "medium"))

    info.add_row(
        "Implementation",
        f"{candidate.get('implementation_loc', 0)} LOC / "
        f"{candidate.get('implementation_files', 0)} files",
    )
    info.add_row(
        "Verifier",
        f"{candidate.get('test_loc', 0)} LOC / {candidate.get('test_files', 0)} files",
    )

    provenance = candidate.get("instruction_provenance") or "—"
    info.add_row(
        "Instruction provenance",
        f"{provenance} — {candidate.get('instruction_source', '') or 'no source'}",
    )

    info.add_row("Status", candidate.get("status", "?"))
    if candidate.get("rejection_reason"):
        info.add_row("Rejection", candidate.get("rejection_reason", ""))

    # Eligibility
    elig_rows = []
    for field in (
        "history",
        "instruction",
        "verifier",
        "environment",
        "oracle",
        "determinism",
        "leakage",
    ):
        val = candidate.get(f"eligibility_{field}")
        if val == 1:
            elig_rows.append(f"[green]✓[/green] {field}")
        elif val == 0:
            elig_rows.append(f"[red]✗[/red] {field}")
        elif val is None:
            elig_rows.append(f"[dim]·[/dim] {field}")
    if elig_rows:
        info.add_row("Eligibility", "\n".join(elig_rows))

    # Leakage
    info.add_row("Leakage risk", f"{candidate.get('leakage_risk', 0):.2f}")
    info.add_row("Network isolation", candidate.get("network_isolation", "NONE"))

    console.print(info)

    # ── Instruction text ───────────────────────────────────────────────────
    instruction = candidate.get("instruction_text")
    if instruction:
        console.print(Panel(instruction, title="Instruction", border_style="cyan"))
    else:
        console.print(
            Panel(
                "[dim]No instruction extracted yet.[/dim]",
                title="Instruction",
                border_style="dim",
            )
        )

    console.print(
        "\n[bold]Next steps:[/bold]\n"
        f"  repobench task validate "
        f"{candidate.get('candidate_id', task_id)}  # run validation pipeline"
    )

    db.close()


def run_task_validate(task_id: str, force: bool = False) -> None:
    """Run the validation pipeline on a candidate task.

    NOTE: This is a stub. The full validation pipeline (no-op, oracle,
    determinism, leakage) is implemented in the validation module and will
    be wired in the validation milestone.
    """
    setup_logging()
    db, repobench_dir = get_database()

    candidate = db.get_candidate(task_id)
    if candidate is None:
        console.print(f"[red]Error:[/red] Candidate not found: {task_id}")
        db.close()
        sys.exit(1)

    status = candidate.get("status")
    if status == "valid" and not force:
        console.print(
            f"[green]Candidate {task_id} is already VALID.[/green]\nUse --force to re-validate."
        )
        db.close()
        return

    console.print(
        Panel(
            f"Candidate: {task_id}\n"
            f"PR:        #{candidate.get('pr_number')}\n"
            f"Type:      {candidate.get('task_type')}\n"
            f"Subsystem: {candidate.get('subsystem')}\n\n"
            "[yellow]Validation pipeline is not yet wired.[/yellow]\n"
            "The validation module (no-op, oracle, determinism, leakage) "
            "will be connected in the validation milestone.",
            title=f"[bold]Validate {task_id}[/bold]",
            border_style="yellow",
        )
    )

    db.close()
