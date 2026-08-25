"""RepoBench Telemetry — manage anonymous telemetry opt-in."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from repobench.logging import setup_logging
from repobench.utils import get_git_root

console = Console()

_TELEMETRY_FILE = ".repobench/telemetry.json"


def _telemetry_path() -> Path:
    cwd = Path.cwd()
    git_root = get_git_root(cwd)
    if git_root is None:
        return cwd / _TELEMETRY_FILE
    return git_root / _TELEMETRY_FILE


def _read_status() -> dict:
    path = _telemetry_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _write_status(enabled: bool) -> None:
    path = _telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": enabled}, indent=2))
    return None


def run_telemetry(action: str) -> None:
    """Enable, disable, or check telemetry status."""
    setup_logging()
    action = action.lower()

    if action == "status":
        status = _read_status()
        enabled = status.get("enabled", False)
        if enabled:
            console.print(
                Panel(
                    "[green]Telemetry: ENABLED[/green]\n\n"
                    "Collected (anonymous only):\n"
                    "  • RepoBench version\n"
                    "  • detected stack\n"
                    "  • PR / candidate counts\n"
                    "  • rejection reasons\n"
                    "  • command failures\n\n"
                    "Never collected:\n"
                    "  • code or sensitive paths\n"
                    "  • PR titles/bodies\n"
                    "  • repository name\n"
                    "  • prompts or agent outputs",
                    title="Telemetry Status",
                    border_style="green",
                )
            )
        else:
            console.print(
                Panel(
                    "[yellow]Telemetry: DISABLED[/yellow]\n\n"
                    "RepoBench does not send any data by default.\n"
                    "Enable with: [bold]repobench telemetry enable[/bold]",
                    title="Telemetry Status",
                    border_style="yellow",
                )
            )
        return

    if action == "enable":
        _write_status(True)
        console.print("[green]Telemetry enabled.[/green]")
        console.print(
            "Only anonymous metrics will be sent: version, stack, PR counts, "
            "rejection reasons, command failures."
        )
        return

    if action == "disable":
        _write_status(False)
        console.print("[yellow]Telemetry disabled.[/yellow]")
        console.print("No data will be sent. Existing local state is preserved.")
        return

    console.print(
        f"[red]Error:[/red] Unknown action: {action}\n"
        "Usage: repobench telemetry [enable|disable|status]"
    )
    sys.exit(1)
