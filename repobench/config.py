"""repobench.yml configuration (PRD §95) and project environment detection (PRD §74)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from repobench.core.errors import ConfigError
from repobench.core.types import ExecutionTarget

CONFIG_FILENAME = "repobench.yml"


class RepositoryConfig(BaseModel):
    provider: str = "github"
    lookback_days: int = 180


class ProjectConfig(BaseModel):
    language: str | None = None
    package_manager: str | None = None
    install_command: str | None = None
    build_command: str | None = None
    test_command: str | None = None
    regression_command: str | None = None


class TaskMiningConfig(BaseModel):
    require_test_change: bool = True
    min_implementation_loc: int = 20
    max_implementation_loc: int = 400
    max_implementation_files: int = 8
    max_test_loc: int | None = None
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
    include_confidence_c: bool = False
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
    environment: Literal["inherit"] = "inherit"
    scrub_ssh_agent: bool = True


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


def _has_package_script(repo: Path, script: str) -> bool:
    data = _read_package_json(repo)
    return bool(data) and script in (data.get("scripts") or {})


def detect_project_environment(repo: Path) -> ProjectConfig:
    """Best-effort environment detection (PRD §74). A suggestion the user can edit."""
    cfg = ProjectConfig()
    has_python = any(
        (repo / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "uv.lock", "poetry.lock")
    )
    has_node = (repo / "package.json").exists()

    if (repo / "uv.lock").exists():
        cfg.language, cfg.package_manager = "python", "uv"
        cfg.install_command = "uv sync --frozen"
        cfg.test_command = "python -m pytest"
        cfg.regression_command = cfg.test_command
    elif (repo / "poetry.lock").exists():
        cfg.language, cfg.package_manager = "python", "poetry"
        cfg.install_command = "poetry install"
        cfg.test_command = "python -m pytest"
        cfg.regression_command = cfg.test_command
    elif has_python:
        cfg.language, cfg.package_manager = "python", "pip"
        cfg.install_command = "python -m pip install -e ."
        cfg.test_command = "python -m pytest"
        cfg.regression_command = cfg.test_command
    elif (repo / "pnpm-lock.yaml").exists():
        cfg.language, cfg.package_manager = "javascript-typescript", "pnpm"
        cfg.install_command = "pnpm install --frozen-lockfile"
    elif (repo / "yarn.lock").exists():
        cfg.language, cfg.package_manager = "javascript-typescript", "yarn"
        cfg.install_command = "yarn install --frozen-lockfile"
    elif has_node:
        cfg.language, cfg.package_manager = "javascript-typescript", "npm"
        cfg.install_command = "npm ci"

    if cfg.language == "javascript-typescript":
        pm = cfg.package_manager or "npm"
        runner = {"pnpm": "pnpm", "yarn": "yarn", "npm": "npx"}.get(pm, "npx")
        framework = _detect_js_test_framework(repo)
        if framework == "vitest":
            cfg.test_command = f"{runner} vitest run"
        elif framework == "jest":
            cfg.test_command = f"{runner} jest"
        elif pm in ("pnpm", "yarn"):
            cfg.test_command = f"{pm} test"
        else:
            cfg.test_command = "npm test"
        cfg.regression_command = cfg.test_command
        if _has_package_script(repo, "build"):
            cfg.build_command = f"{pm} run build"
    return cfg


def default_config_for(repo: Path) -> RepoBenchConfig:
    cfg = RepoBenchConfig()
    cfg.project = detect_project_environment(repo)
    return cfg
