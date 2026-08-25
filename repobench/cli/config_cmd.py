"""RepoBench Config — manage RepoBench configuration."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from repobench.config import get_config_path, load_config, save_config
from repobench.logging import setup_logging
from repobench.utils import get_git_root

console = Console()


def _get_config() -> tuple[Path, Path]:
    """Return (project_root, config_path)."""
    cwd = Path.cwd()
    git_root = get_git_root(cwd)
    if git_root is None:
        console.print("[red]Error:[/red] Not inside a Git repository.")
        sys.exit(1)
    return git_root, get_config_path(git_root)


def run_config_show() -> None:
    """Show current configuration."""
    setup_logging()
    project_root, config_path = _get_config()

    if not config_path.exists():
        console.print(
            "[yellow]No repobench.yml found.[/yellow]\n"
            "Run [bold]repobench init[/bold] to create one."
        )
        return

    content = config_path.read_text()
    syntax = Syntax(content, "yaml", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=f"repobench.yml ({config_path.relative_to(project_root)})"))
    console.print(f"\n[dim]Path: {config_path}[/dim]")


def _set_nested(obj: dict, key: str, value) -> bool:
    """Set a dotted key in a nested dict. Returns True on success."""
    parts = key.split(".")
    cur = obj
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    return True


def _parse_value(value: str):
    """Parse string value into int/float/bool/list where possible."""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if inner:
            return [_parse_value(v.strip()) for v in inner.split(",")]
        return []
    return value


def run_config_set(key: str, value: str) -> None:
    """Set a configuration value and save."""
    setup_logging()
    project_root, config_path = _get_config()

    config = load_config(project_root)
    data = config.model_dump()

    parsed = _parse_value(value)
    _set_nested(data, key, parsed)

    # Validate by round-tripping through the model
    from repobench.models import RepoBenchConfig

    try:
        new_config = RepoBenchConfig(**data)
    except Exception as e:
        console.print(f"[red]Error:[/red] Invalid configuration value: {e}")
        sys.exit(1)

    save_config(new_config, project_root)
    console.print(f"[green]Set[/green] [bold]{key}[/bold] = [cyan]{value}[/cyan]")
    console.print(f"Saved to {config_path.relative_to(project_root)}")
