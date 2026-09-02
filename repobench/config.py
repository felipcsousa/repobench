"""repobench.yml configuration (PRD §95) and project environment detection (PRD §74)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from repobench.core.errors import ConfigError
from repobench.core.types import ExecutionTarget

CONFIG_FILENAME = "repobench.yml"


class RepositoryConfig(BaseModel):
    lookback_days: int = 180


class ProjectConfig(BaseModel):
    language: str | None = None
    package_manager: str | None = None
    install_command: str | None = None
    test_command: str | None = None
    regression_command: str | None = None
    # Issue #34 (monorepos): directory the project commands run in, relative to
    # the repo root. Commands are argv-only (shlex.split), so `cd X && cmd` is
    # impossible — this knob is the only way to point install/test/regression at
    # a sub-project. Harness execution always stays at the workspace root: the
    # agent must see the whole repo (instructions are repo-relative).
    cwd: str | None = None

    @model_validator(mode="after")
    def _validate_cwd(self) -> ProjectConfig:
        """cwd must name a path strictly inside the repository: never absolute,
        never escaping via `..`. Stored normalized and POSIX-relative so command
        runners can join it against any workspace root safely."""
        if self.cwd is None:
            return self
        stripped = self.cwd.strip()
        if not stripped:
            raise ValueError("project.cwd must not be empty — omit it to run at the repo root")
        rel = Path(stripped)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(
                f"project.cwd must be a relative path inside the repository, got {self.cwd!r}"
            )
        self.cwd = rel.as_posix()
        return self


class TaskMiningConfig(BaseModel):
    require_test_change: bool = True
    min_implementation_loc: int = 20
    max_implementation_loc: int = 400
    max_implementation_files: int = 8
    small_loc_max: int = 50
    large_loc_min: int = 200
    large_files_min: int = 5


class BenchmarkDimensions(BaseModel):
    task_type: float = 0.30
    subsystem: float = 0.40
    complexity: float = 0.30


class BenchmarkConfig(BaseModel):
    size: int = 24
    dimensions: BenchmarkDimensions = Field(default_factory=BenchmarkDimensions)
    # None keeps every instruction tier (A/B/C/D); an explicit list (e.g.
    # ["A", "B"]) restricts the benchmark pool to those instruction
    # confidence tiers before validation.
    allowed_confidences: list[str] | None = None


class InstructionGenerationConfig(BaseModel):
    """Tier-D instruction generation (opt-in — it spends real tokens).

    When enabled, `benchmark build` asks the target's harness to draft an
    instruction from the gold IMPLEMENTATION diff for candidates that only have
    the title-derived (confidence C) fallback. The verifier/test patch is never
    part of the generation prompt.
    """

    enabled: bool = False
    # id of a target in cfg.targets that runs the generation
    target: str = ""
    timeout_minutes: int = 5


class ExecutionConfig(BaseModel):
    jobs: int = 1
    timeout_minutes: int = 20
    # Seconds-honest timeout override; wins over timeout_minutes when set.
    # Primarily lets short tests exercise the real timeout path without
    # minute-granularity waits.
    timeout_seconds: int | None = None
    keep_workspaces: bool = False
    # Persisted trust for generic-command targets (PRD §26): the run preview
    # still shows every command template, but the explicit gate is skipped.
    trust_custom_commands: bool = False
    scrub_ssh_agent: bool = True


class AnalysisConfig(BaseModel):
    """Report statistics knobs (PRD §104: the paired-bootstrap seed is stored)."""

    bootstrap_seed: int = 42


class PricingRule(BaseModel):
    input_per_million: float
    cached_input_per_million: float | None = None
    output_per_million: float


class RepoBenchConfig(BaseModel):
    version: int = 1
    repository: RepositoryConfig = Field(default_factory=RepositoryConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    task_mining: TaskMiningConfig = Field(default_factory=TaskMiningConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    instruction_generation: InstructionGenerationConfig = Field(
        default_factory=InstructionGenerationConfig
    )
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    targets: dict[str, ExecutionTarget] = Field(default_factory=dict)
    pricing: dict[str, PricingRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_target_ids(self) -> RepoBenchConfig:
        for name, target in self.targets.items():
            if not target.id:
                target.id = name
        return self

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    def save(self, path: Path) -> None:
        path.write_text(self.to_yaml())

    @classmethod
    def load(cls, path: Path) -> RepoBenchConfig:
        path = Path(path)
        if not path.is_file():
            raise ConfigError(
                f"configuration file not found: {path} (run `repobench init` first)"
            )
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"invalid configuration in {path}: {exc}") from exc


def _read_package_json(repo: Path) -> dict | None:
    pkg = repo / "package.json"
    if not pkg.is_file():
        return None
    try:
        return json.loads(pkg.read_text())
    except Exception:
        return None


def _detect_js_test_framework(repo: Path) -> str | None:
    data = _read_package_json(repo)
    if data is None:
        return None
    deps = {**(data.get("devDependencies") or {}), **(data.get("dependencies") or {})}
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return None


def _has_npm_test_script(directory: Path) -> bool:
    """True only when the directory's package.json actually declares `scripts.test`
    — the guard that keeps `npm test` suggestions from being invented (issue #34)."""
    data = _read_package_json(directory)
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return isinstance(scripts, dict) and "test" in scripts


def _detect_in(directory: Path) -> ProjectConfig:
    """Best-effort environment detection for ONE directory (PRD §74). Root and
    sub-project detection share this single implementation (issue #34) so every
    honesty rule holds at every site — in particular the npm rule: suggest
    `npm test`/`pnpm test`/`yarn test` only when package.json HAS a `test`
    script, else leave test_command None (a None suggestion is rejected honestly
    by validation; an invented one guarantees BASELINE_BROKEN). vitest/jest stay
    suggested from dependencies alone: `npx vitest run` needs no scripts.test.
    A suggestion the user can edit."""
    cfg = ProjectConfig()
    has_python = any(
        (directory / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "uv.lock", "poetry.lock")
    )
    has_node = (directory / "package.json").exists()

    # Detected Python commands use the interpreter running RepoBench: a bare
    # "python" does not exist on many systems (macOS ships python3 only), and a
    # dead suggestion rejects every task with ENVIRONMENT_UNSUPPORTED at build.
    py = shlex.quote(sys.executable)

    if (directory / "uv.lock").exists():
        cfg.language, cfg.package_manager = "python", "uv"
        cfg.install_command = "uv sync --frozen"
        cfg.test_command = f"{py} -m pytest"
        cfg.regression_command = cfg.test_command
    elif (directory / "poetry.lock").exists():
        cfg.language, cfg.package_manager = "python", "poetry"
        cfg.install_command = "poetry install"
        cfg.test_command = f"{py} -m pytest"
        cfg.regression_command = cfg.test_command
    elif has_python:
        cfg.language, cfg.package_manager = "python", "pip"
        cfg.install_command = f"{py} -m pip install -e ."
        cfg.test_command = f"{py} -m pytest"
        cfg.regression_command = cfg.test_command
    elif (directory / "pnpm-lock.yaml").exists():
        cfg.language, cfg.package_manager = "javascript-typescript", "pnpm"
        cfg.install_command = "pnpm install --frozen-lockfile"
    elif (directory / "yarn.lock").exists():
        cfg.language, cfg.package_manager = "javascript-typescript", "yarn"
        cfg.install_command = "yarn install --frozen-lockfile"
    elif has_node:
        cfg.language, cfg.package_manager = "javascript-typescript", "npm"
        cfg.install_command = "npm ci"

    if cfg.language == "javascript-typescript":
        pm = cfg.package_manager or "npm"
        runner = {"pnpm": "pnpm", "yarn": "yarn", "npm": "npx"}.get(pm, "npx")
        framework = _detect_js_test_framework(directory)
        if framework == "vitest":
            cfg.test_command = f"{runner} vitest run"
        elif framework == "jest":
            cfg.test_command = f"{runner} jest"
        elif _has_npm_test_script(directory):
            cfg.test_command = f"{pm} test" if pm in ("pnpm", "yarn") else "npm test"
        cfg.regression_command = cfg.test_command
    return cfg


def detect_project_environment(repo: Path) -> ProjectConfig:
    """Best-effort environment detection for the repo root (PRD §74)."""
    return _detect_in(repo)


# Conservative monorepo scan roots (issue #34), documented on purpose: common
# layouts only. Top-level names are probed as projects directly; globbed names
# have each child probed. Anything else (src/, libs/, gen/ …) is deliberately
# not guessed — a false project line costs trust too.
SUBPROJECT_TOP_LEVEL_DIRS = ("backend", "api", "server")
SUBPROJECT_GLOBBED_DIRS = ("apps", "packages", "services")

# Dependency/venv junk that must never surface as a project.
_DETECTION_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".tox",
        ".eggs",
        "dist",
        "build",
        ".git",
    }
)


class DetectedProject(BaseModel):
    """A sub-project detected one level below the repo root (issue #34)."""

    path: str  # POSIX-style, relative to the repo root ("backend", "apps/api")
    config: ProjectConfig


def detect_subprojects(repo: Path) -> list[DetectedProject]:
    """Sub-projects with their own runners, ONE level below the repo root
    (issue #34). Candidates: backend/ api/ server/ at the top level plus every
    child of apps/ packages/ services/ (see SUBPROJECT_*_DIRS for the
    conservative, documented list). Junk dirs (node_modules, venvs) are skipped,
    and the repo root is never included — it is already the primary project.
    Detection is shared with the root via `_detect_in`, so the npm test-script
    rule applies identically."""
    candidates: list[Path] = [repo / name for name in SUBPROJECT_TOP_LEVEL_DIRS]
    for name in SUBPROJECT_GLOBBED_DIRS:
        globbed = repo / name
        if globbed.is_dir():
            candidates.extend(sorted(globbed.iterdir()))
    detected: list[DetectedProject] = []
    for candidate in candidates:
        if not candidate.is_dir() or candidate.name in _DETECTION_SKIP_DIRS:
            continue
        cfg = _detect_in(candidate)
        if cfg.language is None:
            continue
        detected.append(
            DetectedProject(path=candidate.relative_to(repo).as_posix(), config=cfg)
        )
    detected.sort(key=lambda project: project.path)
    return detected


def compose_cwd(base: Path, project: ProjectConfig) -> Path:
    """Directory project commands run in (issue #34): `base`, or
    `base/project.cwd` when configured. cwd is validated relative by
    ProjectConfig, so the join cannot escape `base` — but a materialized
    workspace may still lack the directory, so callers must check existence
    before running and degrade with a clear message, never a crash."""
    return base / project.cwd if project.cwd else base


def default_config_for(repo: Path) -> RepoBenchConfig:
    cfg = RepoBenchConfig()
    cfg.project = detect_project_environment(repo)
    # Issue #34: sub-projects are surfaced by doctor/init (so the ignored
    # backend is visible before the first build) but never auto-selected —
    # benchmarking only the backend is the user's decision, via project.cwd.
    return cfg
