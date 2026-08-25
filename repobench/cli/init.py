"""RepoBench Init — initialize RepoBench in the current repository."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from repobench.config import save_config
from repobench.logging import get_logger, setup_logging
from repobench.models import ProjectConfig, RepoBenchConfig, RepositoryConfig
from repobench.storage.database import Database
from repobench.utils import (
    get_git_root,
    get_github_owner_repo,
    has_github_remote,
    run_cmd_safe,
)

logger = get_logger("init")
console = Console()

_REPOBENCH_DIR = ".repobench"
_CONFIG_FILE = "repobench.yml"


def _detect_languages(cwd: Path) -> list[str]:
    """Detect primary languages in the repository."""
    langs: list[str] = []
    py_files = list(cwd.rglob("*.py"))
    ts_files = list(cwd.rglob("*.ts"))
    tsx_files = list(cwd.rglob("*.tsx"))
    js_files = list(cwd.rglob("*.js"))
    go_files = list(cwd.rglob("*.go"))
    rs_files = list(cwd.rglob("*.rs"))

    # Filter out common non-source dirs
    exclude_dirs = {"node_modules", ".git", "venv", ".venv", "dist", "build", "__pycache__"}

    def _count_real(files: list[Path]) -> int:
        return sum(1 for f in files if not any(d in f.parts for d in exclude_dirs))

    java_files = list(cwd.rglob("*.java"))

    counts = [
        ("python", _count_real(py_files)),
        ("typescript", _count_real(ts_files) + _count_real(tsx_files)),
        ("javascript", _count_real(js_files)),
        ("go", _count_real(go_files)),
        ("rust", _count_real(rs_files)),
        ("java", _count_real(java_files)),
    ]

    for lang, count in sorted(counts, key=lambda x: -x[1]):
        if count > 0:
            langs.append(lang)

    return langs[:3] if langs else ["unknown"]


def _detect_pkg_manager(cwd: Path) -> tuple[str | None, str | None, str | None]:
    """Detect package manager and return (install, build, test) commands."""
    # Node ecosystem
    if (cwd / "pnpm-lock.yaml").exists():
        install = "pnpm install --frozen-lockfile"
        build = None
        test = None
        pkg_json = cwd / "package.json"
        if pkg_json.exists():
            try:
                scripts = json.loads(pkg_json.read_text()).get("scripts", {})
                if "build" in scripts:
                    build = "pnpm build"
                if "test" in scripts:
                    test = "pnpm test --run"
            except (ValueError, KeyError):
                pass
        return install, build, test

    if (cwd / "yarn.lock").exists():
        return "yarn install --frozen-lockfile", None, None
    if (cwd / "package-lock.json").exists():
        return "npm ci", None, None

    # Go ecosystem
    if (cwd / "go.mod").exists():
        return "go mod download", "go build ./...", "go test ./..."

    # Java — Maven
    if (cwd / "pom.xml").exists():
        return "mvn dependency:resolve -q", "mvn compile -q", "mvn test -q"

    # Java — Gradle
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        if (cwd / "gradlew").exists():
            return (
                "./gradlew dependencies --quiet",
                "./gradlew compileJava --quiet",
                "./gradlew test --quiet",
            )
        return (
            "gradle dependencies --quiet",
            "gradle compileJava --quiet",
            "gradle test --quiet",
        )

    # Python ecosystem
    if (cwd / "uv.lock").exists():
        return "uv sync", None, None
    if (cwd / "poetry.lock").exists():
        return "poetry install", None, None
    if (cwd / "requirements.txt").exists():
        return "pip install -r requirements.txt", None, None

    return None, None, None


def _detect_test_command(cwd: Path) -> str | None:
    """Detect test command from project files."""
    if (cwd / "pytest.ini").exists() or (cwd / "conftest.py").exists():
        return "pytest"
    if any(cwd.glob("vitest.config.*")):
        return "vitest run"
    if any(cwd.glob("jest.config.*")):
        return "jest"
    # Check pyproject.toml for pytest config
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            if "[tool.pytest" in content:
                return "pytest"
        except Exception:
            pass

    # Go — native test runner
    if (cwd / "go.mod").exists():
        return "go test ./..."

    # Java — Maven or Gradle
    if (cwd / "pom.xml").exists():
        return "mvn test -q"
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        if (cwd / "gradlew").exists():
            return "./gradlew test --quiet"
        return "gradle test --quiet"

    return None


def _add_to_gitignore(project_root: Path) -> bool:
    """Add .repobench/ to .gitignore if not present."""
    gitignore = project_root / ".gitignore"
    entry = f"\n{_REPOBENCH_DIR}/\n"

    if gitignore.exists():
        content = gitignore.read_text()
        if _REPOBENCH_DIR in content:
            return False
        if not content.endswith("\n"):
            entry = "\n" + entry
        gitignore.write_text(content + entry)
    else:
        gitignore.write_text(f"{_REPOBENCH_DIR}/\n")
    return True


def run_init(
    lookback_days: int = 180,
    force: bool = False,
    add_gitignore: bool = True,
) -> None:
    """Initialize RepoBench in the current repository."""
    setup_logging()
    cwd = Path.cwd()

    # ── Validate we're in a git repo ───────────────────────────────────────
    git_root = get_git_root(cwd)
    if git_root is None:
        console.print("[red]Error:[/red] Not inside a Git repository.")
        sys.exit(1)

    # ── Check for existing config ──────────────────────────────────────────
    config_path = git_root / _CONFIG_FILE
    if config_path.exists() and not force:
        console.print(
            f"[yellow]repobench.yml already exists in {git_root}.[/yellow]\n"
            "Use --force to overwrite."
        )
        sys.exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Detecting repository...", total=None)

        # ── Detect GitHub info ─────────────────────────────────────────────
        owner_repo = get_github_owner_repo(git_root)
        gh_remote = has_github_remote(git_root)

        repo_remote = ""
        repo_provider = "github"
        if owner_repo:
            repo_remote = f"github.com/{owner_repo[0]}/{owner_repo[1]}"
        elif gh_remote:
            # Fallback: try to get any remote
            ok, out, _ = run_cmd_safe(["git", "remote", "get-url", "origin"], cwd=git_root)
            if ok:
                repo_remote = out.strip()

        progress.update(task, description="Detecting languages...")
        languages = _detect_languages(git_root)

        progress.update(task, description="Detecting package manager...")
        install_cmd, build_cmd, _ = _detect_pkg_manager(git_root)

        progress.update(task, description="Detecting test framework...")
        test_cmd = _detect_test_command(git_root)

    # ── Build config ───────────────────────────────────────────────────────
    config = RepoBenchConfig(
        repository=RepositoryConfig(
            provider=repo_provider,
            lookback_days=lookback_days,
        ),
        project=ProjectConfig(
            languages=languages,
            install_command=install_cmd,
            build_command=build_cmd,
            test_command=test_cmd,
        ),
    )

    # ── Create directories ─────────────────────────────────────────────────
    repobench_dir = git_root / _REPOBENCH_DIR
    repobench_dir.mkdir(parents=True, exist_ok=True)
    (repobench_dir / "cache").mkdir(exist_ok=True)
    (repobench_dir / "logs").mkdir(exist_ok=True)
    (repobench_dir / "benchmarks").mkdir(exist_ok=True)

    # ── Initialize database ────────────────────────────────────────────────
    db_path = repobench_dir / "state.db"
    db = Database(db_path)
    db.initialize()
    db.close()

    # ── Save config ────────────────────────────────────────────────────────
    save_config(config, git_root)

    # ── Update .gitignore ──────────────────────────────────────────────────
    gitignore_updated = False
    if add_gitignore:
        gitignore_updated = _add_to_gitignore(git_root)

    # ── Print summary ──────────────────────────────────────────────────────
    console.print()
    table_content = []
    if repo_remote:
        table_content.append(f"  [cyan]Repository:[/cyan] {repo_remote}")
    table_content.append(f"  [cyan]Languages:[/cyan] {', '.join(languages)}")
    if install_cmd:
        table_content.append(f"  [cyan]Install:[/cyan] {install_cmd}")
    if build_cmd:
        table_content.append(f"  [cyan]Build:[/cyan] {build_cmd}")
    if test_cmd:
        table_content.append(f"  [cyan]Test:[/cyan] {test_cmd}")
    table_content.append(f"  [cyan]Lookback:[/cyan] {lookback_days} days")
    table_content.append(f"  [cyan]Config:[/cyan] {config_path.relative_to(git_root)}")
    table_content.append("  [cyan]Database:[/cyan] .repobench/state.db")
    if gitignore_updated:
        table_content.append("  [cyan].gitignore:[/cyan] updated")

    console.print(
        Panel(
            "\n".join(table_content),
            title="[bold green]RepoBench Initialized[/bold green]",
            border_style="green",
        )
    )

    console.print(
        "\n[bold]Next steps:[/bold]\n"
        "  repobench doctor       # verify prerequisites\n"
        "  repobench analyze      # analyze repository workload\n"
        "  repobench candidates   # view mined candidate tasks\n"
    )
