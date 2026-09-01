"""Shared fixtures: the deterministic fixture repository and the fake agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.fixture_repo import build_fixture_repo

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def fixture_repo_builder(tmp_path: Path):
    """Builder creating a fixture repo under tmp_path; returns the repo path."""

    def _build(name: str = "repo") -> Path:
        return build_fixture_repo(tmp_path / name)

    return _build


@pytest.fixture()
def fixture_repo(fixture_repo_builder) -> Path:
    return fixture_repo_builder()


@pytest.fixture()
def fake_agent_path() -> Path:
    """Path to the fake coding-agent script (modes via argv or RB_FAKE_MODE)."""
    return FIXTURES_DIR / "fake_agent.py"
