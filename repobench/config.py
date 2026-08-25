"""Configuration loading and saving for RepoBench."""

from __future__ import annotations

from pathlib import Path

import yaml

from repobench.models import RepoBenchConfig

_CONFIG_FILENAME = "repobench.yml"


def get_config_path(project_root: Path) -> Path:
    return project_root / _CONFIG_FILENAME


def load_config(project_root: Path) -> RepoBenchConfig:
    config_path = get_config_path(project_root)
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return RepoBenchConfig(**raw)
    return RepoBenchConfig()


def save_config(config: RepoBenchConfig, project_root: Path) -> Path:
    config_path = get_config_path(project_root)
    data = config.model_dump(exclude_defaults=False)
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return config_path
