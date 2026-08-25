"""RepoBench Candidates — show candidate tasks mined from repository history."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from repobench.logging import setup_logging
from repobench.storage.database import Database

console = Console()


def run_candidates(status_filter: str | None = None, verbose: bool = False) -> None:
    """Show candidate tasks and rejection reason counts."""
    setup_logging(verbose=verbose)
    cwd = Path.cwd()
    repobench_dir = cwd / ".repobench"

    # Find .repobench in git root if not in cwd
    if not repobench_dir.exists():
        from repobench.utils import get_git_root

        git_root = get_git_root(cwd)
        if git_root:
            repobench_dir = git_root / ".repobench"

    if not repobench_dir.exists():
        console.print(
            "[red]Error:[/red] RepoBench not initialized. Run [bold]repobench init[/bold] first."
        )
        sys.exit(1)

    db_path = repobench_dir / "state.db"
    db = Database(db_path)
    db.initialize()

    # ── Status counts ──────────────────────────────────────────────────────
    status_counts = db.count_candidates_by_status()
    if not status_counts:
        console.print(
            "[yellow]No candidates found yet.[/yellow]\n"
            "Run [bold]repobench analyze[/bold] to mine candidate tasks."
        )
        db.close()
        return

    status_table = Table(title="Candidates by Status", show_header=True, header_style="bold")
    status_table.add_column("Status", style="cyan")
    status_table.add_column("Count", justify="right")

    for status in sorted(status_counts, key=lambda s: -status_counts[s]):
        status_table.add_row(status, str(status_counts[status]))
    console.print(status_table)

    # ── Rejection reasons (if not filtering) ───────────────────────────────
    if not status_filter:
        rejected = db.get_candidates_by_status("rejected")
        if rejected:
            reason_counts: dict[str, int] = {}
            for cand in rejected:
                reason = cand.get("rejection_reason") or "OTHER"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

            reason_table = Table(title="Rejection Reasons", show_header=True, header_style="bold")
            reason_table.add_column("Reason", style="cyan")
            reason_table.add_column("Count", justify="right")

            for reason in sorted(reason_counts, key=lambda r: -reason_counts[r]):
                reason_table.add_row(reason, str(reason_counts[reason]))
            console.print(reason_table)

    # ── Candidate listing ──────────────────────────────────────────────────
    if status_filter:
        candidates = db.get_candidates_by_status(status_filter)
    else:
        # Show valid + all discovered candidates
        candidates = db.get_candidates_by_status("valid")
        discovered = db.get_candidates_by_status("discovered")
        candidates.extend(discovered)

    if candidates:
        cand_table = Table(
            title=f"Candidates ({len(candidates)})", show_header=True, header_style="bold"
        )
        cand_table.add_column("ID", style="cyan")
        cand_table.add_column("PR", justify="right")
        cand_table.add_column("Type", style="magenta")
        cand_table.add_column("Subsystem", style="green")
        cand_table.add_column("Complexity", style="yellow")
        cand_table.add_column("Impl LOC", justify="right")
        cand_table.add_column("Test LOC", justify="right")
        cand_table.add_column("Status", style="bold")

        for cand in candidates:
            cand_table.add_row(
                cand.get("candidate_id", "?"),
                str(cand.get("pr_number", "")),
                cand.get("task_type", "unknown"),
                cand.get("subsystem", "unknown"),
                cand.get("complexity", "medium"),
                str(cand.get("implementation_loc", 0)),
                str(cand.get("test_loc", 0)),
                cand.get("status", "?"),
            )
        console.print(cand_table)

    if not status_filter:
        console.print(
            "\n[dim]Tip:[/dim] Use [bold]repobench candidates --status valid[/bold] to filter.\n"
            "[dim]Inspect:[/dim] [bold]repobench task inspect <id>[/bold]"
        )

    db.close()
