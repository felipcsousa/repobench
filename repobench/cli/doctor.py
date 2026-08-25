"""RepoBench Doctor — check prerequisites for RepoBench."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from repobench.logging import get_logger, setup_logging
from repobench.utils import (
    get_github_owner_repo,
    has_github_remote,
    is_git_repo,
    run_cmd_safe,
)

logger = get_logger("doctor")
console = Console()


def _check(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "warn_only": warn_only}


def run_doctor(verbose: bool = False) -> None:
    """Run all prerequisite checks and print results."""
    setup_logging(verbose=verbose)
    cwd = Path.cwd()

    checks: list[dict] = []

    # ── Repository checks ──────────────────────────────────────────────────
    checks.append(_check("Git repository detected", is_git_repo(cwd)))
    checks.append(_check("GitHub remote detected", has_github_remote(cwd)))

    gh_ok, gh_out, gh_err = run_cmd_safe(["gh", "auth", "status"])
    checks.append(
        _check("GitHub authenticated (gh auth)", gh_ok, detail=gh_err if not gh_ok else "")
    )

    # Count merged PRs if github is available
    pr_count: int | None = None
    owner_repo = get_github_owner_repo(cwd)
    if owner_repo and gh_ok:
        owner, repo = owner_repo
        count_ok, count_out, _ = run_cmd_safe(
            ["gh", "api", f"repos/{owner}/{repo}", "--jq", ".private"],
        )
        if count_ok:
            pr_count_text, _, _ = run_cmd_safe(
                [
                    "gh",
                    "api",
                    f"search/issues?q=repo:{owner}/{repo}+type:pr+is:merged",
                    "--jq",
                    ".total_count",
                ]
            )
            if pr_count_text:
                try:
                    pr_count = int(pr_count_text.strip())
                except ValueError:
                    pass

    # ── Runtime checks ─────────────────────────────────────────────────────
    python_ok = sys.version_info >= (3, 12)
    python_detail = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        _check("Python >= 3.12", python_ok, detail=python_detail, warn_only=not python_ok)
    )

    docker_ok = shutil.which("docker") is not None
    checks.append(_check("Docker", docker_ok, warn_only=not docker_ok))

    harbor_ok = shutil.which("harbor") is not None
    if not harbor_ok:
        # Try checking via pip
        h_ok, h_out, _ = run_cmd_safe([sys.executable, "-m", "pip", "show", "harbor-cli"])
        harbor_ok = h_ok
    checks.append(_check("Harbor", harbor_ok, warn_only=not harbor_ok))

    # Go toolchain
    if (cwd / "go.mod").exists():
        go_ok = shutil.which("go") is not None
        go_detail = ""
        if go_ok:
            g_ok, g_out, _ = run_cmd_safe(["go", "version"])
            go_detail = g_out.strip() if g_ok else ""
        checks.append(_check("Go toolchain", go_ok, detail=go_detail, warn_only=not go_ok))

    # Java toolchain
    has_java_project = (
        (cwd / "pom.xml").exists()
        or (cwd / "build.gradle").exists()
        or (cwd / "build.gradle.kts").exists()
    )
    if has_java_project:
        java_ok = shutil.which("java") is not None
        java_detail = ""
        if java_ok:
            j_ok, j_out, _ = run_cmd_safe(["java", "-version"])
            java_detail = j_out.strip().splitlines()[0] if j_ok and j_out.strip() else ""
        checks.append(_check("Java/JDK", java_ok, detail=java_detail, warn_only=not java_ok))

        # Maven or Gradle
        if (cwd / "pom.xml").exists():
            mvn_ok = shutil.which("mvn") is not None
            checks.append(_check("Maven", mvn_ok, warn_only=not mvn_ok))
        if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
            gradle_ok = (cwd / "gradlew").exists() or shutil.which("gradle") is not None
            checks.append(_check("Gradle", gradle_ok, warn_only=not gradle_ok))

    # ── Project detection ───────────────────────────────────────────────────
    languages: list[str] = []
    pkg_managers: list[str] = []
    test_frameworks: list[str] = []

    if (cwd / "pyproject.toml").exists():
        languages.append("Python")
    if any((cwd / f).exists() for f in ("setup.py", "setup.cfg")):
        languages.append("Python")
    if (cwd / "package.json").exists():
        languages.append("TypeScript/JavaScript")
    if (cwd / "tsconfig.json").exists():
        languages.append("TypeScript")
    if (cwd / "go.mod").exists():
        languages.append("Go")
    has_java_project = (
        (cwd / "pom.xml").exists()
        or (cwd / "build.gradle").exists()
        or (cwd / "build.gradle.kts").exists()
    )
    if has_java_project:
        languages.append("Java")

    if (cwd / "pnpm-lock.yaml").exists():
        pkg_managers.append("pnpm")
    if (cwd / "yarn.lock").exists():
        pkg_managers.append("yarn")
    if (cwd / "package-lock.json").exists():
        pkg_managers.append("npm")
    if (cwd / "uv.lock").exists():
        pkg_managers.append("uv")
    if (cwd / "poetry.lock").exists():
        pkg_managers.append("poetry")
    if (cwd / "requirements.txt").exists():
        pkg_managers.append("pip")
    if (cwd / "go.mod").exists():
        pkg_managers.append("go")
    if (cwd / "pom.xml").exists():
        pkg_managers.append("maven")
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        pkg_managers.append("gradle")

    if any((cwd / f).exists() for f in ("pytest.ini", "conftest.py", "pyproject.toml")):
        test_frameworks.append("pytest")
    if any(cwd.glob("vitest.config.*")):
        test_frameworks.append("Vitest")
    if any(cwd.glob("jest.config.*")):
        test_frameworks.append("Jest")
    if (cwd / "go.mod").exists():
        test_frameworks.append("go test")
    if (cwd / "pom.xml").exists():
        test_frameworks.append("Maven/Surefire")
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        test_frameworks.append("Gradle Test")

    # Detect build/test commands from package.json
    build_cmd = None
    test_cmd = None
    pkg_json_path = cwd / "package.json"
    if pkg_json_path.exists():
        import json as _json

        try:
            pkg_data = _json.loads(pkg_json_path.read_text())
            scripts = pkg_data.get("scripts", {})
            if "build" in scripts:
                build_cmd = "pnpm build" if "pnpm" in pkg_managers else "npm run build"
            if "test" in scripts:
                test_cmd = "pnpm test" if "pnpm" in pkg_managers else "npm test"
        except (ValueError, KeyError):
            pass

    # ── Build table ────────────────────────────────────────────────────────
    table = Table(title="RepoBench Doctor", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="bold", min_width=30)
    table.add_column("Status", min_width=10)
    table.add_column("Detail", min_width=20)

    for check in checks:
        if check["ok"]:
            status = "[green]✓[/green]"
        elif check.get("warn_only"):
            status = "[yellow]⚠[/yellow]"
        else:
            status = "[red]✗[/red]"
        table.add_row(check["name"], status, check.get("detail", ""))

    # Project info rows
    if languages:
        table.add_row("Languages", "[cyan]" + ", ".join(languages) + "[/cyan]")
    if pkg_managers:
        table.add_row("Package manager", "[cyan]" + ", ".join(pkg_managers) + "[/cyan]")
    if test_frameworks:
        table.add_row("Test framework", "[cyan]" + ", ".join(test_frameworks) + "[/cyan]")
    if build_cmd:
        table.add_row("Build command", f"[dim]{build_cmd}[/dim]")
    if test_cmd:
        table.add_row("Test command", f"[dim]{test_cmd}[/dim]")
    if pr_count is not None:
        table.add_row("Merged PRs", f"[cyan]{pr_count:,}[/cyan]")
    if owner_repo:
        table.add_row("Repository", f"[cyan]{owner_repo[0]}/{owner_repo[1]}[/cyan]")

    console.print(table)

    # ── Summary ────────────────────────────────────────────────────────────
    blocking = [c for c in checks if not c["ok"] and not c.get("warn_only")]
    warnings = [c for c in checks if not c["ok"] and c.get("warn_only")]

    if not blocking:
        console.print(Panel("[green]Ready for analysis.[/green]", border_style="green"))
    else:
        console.print(
            Panel(
                f"[red]Blocked: {len(blocking)} critical check(s) failed.[/red]\n"
                + "\n".join(f"  • {c['name']}" for c in blocking),
                border_style="red",
            )
        )

    if warnings:
        console.print(
            Panel(
                f"[yellow]Warnings: {len(warnings)} optional check(s) failed.[/yellow]\n"
                + "\n".join(f"  • {c['name']}" for c in warnings),
                border_style="yellow",
            )
        )

    if blocking:
        sys.exit(1)
