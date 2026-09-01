"""CLI tests (PRD §90-95): doctor, init, targets and candidates via the Typer
CliRunner — hermetic, no network, no harness binaries required."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from repobench.cli.app import app
from repobench.config import ProjectConfig, RepoBenchConfig
from repobench.core.types import ExecutionTarget
from repobench.storage.db import Storage

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _write_config(repo: Path, **targets: ExecutionTarget) -> RepoBenchConfig:
    cfg = RepoBenchConfig()
    cfg.project = ProjectConfig(
        language="python",
        test_command="python -m pytest",
        regression_command="python -m pytest",
    )
    cfg.targets.update(targets)
    cfg.save(repo / "repobench.yml")
    return cfg


# --------------------------------------------------------------------- doctor


def test_doctor_exits_zero_in_fixture_repo_and_reports_sections(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    result = _invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "RepoBench Doctor" in result.output
    assert "Repository" in result.output
    assert "Python" in result.output  # project detection (pyproject.toml fixture)
    assert "Harnesses" in result.output
    assert "no inference" in result.output.lower() or "PRD" in result.output


def test_doctor_harnesses_flag_prints_capability_table(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    result = _invoke("doctor", "--harnesses")
    assert result.exit_code == 0, result.output
    for column in ("MODEL", "JSON", "TOKENS", "COST", "PROVIDER"):
        assert column in result.output
    for harness in ("claude", "codex", "opencode", "gemini", "command"):
        assert harness in result.output


def test_doctor_outside_a_repo_still_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "not inside a git repository" in result.output


# ----------------------------------------------------------------------- init


def test_init_writes_config_and_gitignore(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    result = _invoke("init", "--yes")
    assert result.exit_code == 0, result.output
    config = fixture_repo / "repobench.yml"
    assert config.is_file()
    content = config.read_text()
    assert "test_command" in content  # detected Python project commands suggested
    gitignore = (fixture_repo / ".gitignore").read_text()
    assert ".repobench/" in gitignore.split()
    assert (fixture_repo / ".repobench").is_dir()
    assert "Detected project commands" in result.output


def test_init_refuses_overwrite_without_force(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    before = (fixture_repo / "repobench.yml").read_text()
    refused = _invoke("init", "--yes")
    assert refused.exit_code == 1
    assert "--force" in refused.output
    assert (fixture_repo / "repobench.yml").read_text() == before

    forced = _invoke("init", "--yes", "--force")
    assert forced.exit_code == 0, forced.output
    assert "Config overwritten" in forced.output


def test_init_outside_git_repo_fails_politely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _invoke("init", "--yes")
    assert result.exit_code == 1
    assert "not inside a git repository" in result.output
    assert not (tmp_path / "repobench.yml").exists()


# -------------------------------------------------------------------- targets


def test_targets_list_shows_provider_resolution(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(
        fixture_repo,
        claude=ExecutionTarget(harness="claude"),
        glm=ExecutionTarget(harness="opencode", model="zai/glm-x"),
        minimax=ExecutionTarget(harness="opencode", model="openrouter/minimax-x", provider="openrouter"),
    )
    result = _invoke("targets", "list")
    assert result.exit_code == 0, result.output
    for token in ("claude", "glm", "opencode", "zai/glm-x", "zai", "openrouter", "inherited"):
        assert token in result.output


def test_targets_validate_structural_only(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo, glm=ExecutionTarget(harness="opencode", model="zai/glm-x"))
    ok = _invoke("targets", "validate", "glm")
    assert ok.exit_code == 0, ok.output
    assert "structurally valid" in ok.output

    unknown = _invoke("targets", "validate", "nope")
    assert unknown.exit_code == 1
    assert "unknown target" in unknown.output
    assert "glm" in unknown.output  # available ids are listed


def test_targets_validate_rejects_command_target_without_command(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo, broken=ExecutionTarget(harness="command"))
    result = _invoke("targets", "validate", "broken")
    assert result.exit_code == 1
    assert "invalid" in result.output


# ----------------------------------------------------------------- candidates


def test_candidates_after_analyze(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    analyzed = _invoke("analyze")
    assert analyzed.exit_code == 0, analyzed.output
    assert "RepoBench analyzed your repository" in analyzed.output
    assert "Merged PRs" in analyzed.output
    assert "No inference tokens were consumed." in analyzed.output

    listing = _invoke("candidates")
    assert listing.exit_code == 0, listing.output
    assert "DISCOVERED" in listing.output
    assert "NO_TEST_CHANGE" in listing.output

    only_filtered = _invoke("candidates", "--status", "FILTERED")
    assert only_filtered.exit_code == 0
    assert "NO_TEST_CHANGE" in only_filtered.output
    assert "DISCOVERED" not in only_filtered.output

    storage = Storage(fixture_repo / ".repobench" / "state.db")
    statuses = {c.pr.number: c.status.value for c in storage.list_candidates()}
    assert statuses == {7: "DISCOVERED", 8: "FILTERED"}


def test_candidates_without_analyze_hints_at_analyze(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    result = _invoke("candidates")
    assert result.exit_code == 0
    assert "repobench analyze" in result.output
