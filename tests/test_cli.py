"""CLI tests (PRD §90-95): doctor, init, targets and candidates via the Typer
CliRunner — hermetic, no network, no harness binaries required."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repobench.cli.app import app
from repobench.config import ProjectConfig, RepoBenchConfig
from repobench.core.types import ExecutionTarget
from repobench.storage.db import Storage
from tests.fixtures.gitutil import commit_all, git

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


# ------------------------------------------- monorepo detection (issue #34)


def _monorepo_repo(tmp_path: Path, *, root_test_script: bool) -> Path:
    """The lumpfish shape: npm root project + python backend/, committed as a
    real git repo so doctor/init can find the root."""
    repo = tmp_path / "mono"
    repo.mkdir()
    scripts = {"test": "jest --ci"} if root_test_script else {"build": "next build"}
    (repo / "package.json").write_text(json.dumps({"name": "mono", "scripts": scripts}))
    backend = repo / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    commit_all(repo, "initial")
    return repo


def test_doctor_surfaces_subprojects_and_monorepo_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _monorepo_repo(tmp_path, root_test_script=True)
    monkeypatch.chdir(repo)
    result = _invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "JavaScript/TypeScript" in result.output
    assert "↳ backend" in result.output
    assert "Python" in result.output
    # The doctor suggests the interpreter running RepoBench (shlex.quoted), so on
    # Windows the line reads '<path>\python.exe' -m pytest — assert the pytest
    # intent, not the POSIX spelling.
    assert "-m pytest" in result.output
    assert "project.cwd" in result.output  # the monorepo hint names the knob


def test_doctor_flags_missing_root_test_command_without_inventing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _monorepo_repo(tmp_path, root_test_script=False)
    monkeypatch.chdir(repo)
    result = _invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "no test command" in result.output
    assert "npm test" not in result.output  # never print an invented command
    assert "↳ backend" in result.output  # the ignored backend is still visible


def test_init_lists_subprojects_and_never_autosets_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _monorepo_repo(tmp_path, root_test_script=False)
    monkeypatch.chdir(repo)
    result = _invoke("init", "--yes")
    assert result.exit_code == 0, result.output
    assert "sub-projects detected" in result.output
    assert "backend" in result.output
    assert "project.cwd" in result.output
    assert "none detected" in result.output  # the root's honest None suggestion

    content = (repo / "repobench.yml").read_text()
    assert "language: javascript-typescript" in content
    assert "test_command: null" in content  # the lumpfish guarantee
    assert "npm test" not in content
    # never auto-set: benchmarking only the backend is the user's decision
    cwd_lines = [line for line in content.splitlines() if line.strip().startswith("cwd:")]
    assert all("null" in line for line in cwd_lines)


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


# ----------------------------------------------------------------- targets add


def test_targets_add_writes_command_target_to_yml(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    result = _invoke(
        "targets",
        "add",
        "stub",
        "--harness",
        "command",
        "--command",
        "python",
        "--command",
        "fake_agent.py",
    )
    assert result.exit_code == 0, result.output
    assert "Next: repobench run stub" in result.output
    assert "rewritten" in result.output  # the yml was re-serialized
    assert "--trust-custom-command" in result.output  # the PRD §26 gate reminder
    cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
    assert cfg.targets["stub"].harness == "command"
    assert cfg.targets["stub"].command == ["python", "fake_agent.py"]


def test_targets_add_refuses_duplicate_id_without_force(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo, glm=ExecutionTarget(harness="opencode", model="zai/glm-x"))
    refused = _invoke("targets", "add", "glm", "--harness", "codex")
    assert refused.exit_code == 1
    assert "--force" in refused.output
    cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
    assert cfg.targets["glm"].harness == "opencode"  # untouched

    forced = _invoke("targets", "add", "glm", "--harness", "codex", "--force")
    assert forced.exit_code == 0, forced.output
    cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
    assert cfg.targets["glm"].harness == "codex"


def test_targets_add_unknown_harness_lists_known_ones(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    result = _invoke("targets", "add", "nope", "--harness", "cursor")
    assert result.exit_code == 1
    assert "unknown harness" in result.output
    for known in ("claude", "codex", "opencode", "gemini", "command"):
        assert known in result.output
    assert "nope" not in RepoBenchConfig.load(fixture_repo / "repobench.yml").targets


def test_targets_add_command_harness_requires_command(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    result = _invoke("targets", "add", "broken", "--harness", "command")
    assert result.exit_code == 1
    assert "invalid" in result.output  # structural validation catches it
    assert "broken" not in RepoBenchConfig.load(fixture_repo / "repobench.yml").targets


def test_targets_add_harness_target_needs_no_command(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    result = _invoke(
        "targets", "add", "fast", "--harness", "codex", "--model", "gpt-5.3-codex"
    )
    assert result.exit_code == 0, result.output
    target = RepoBenchConfig.load(fixture_repo / "repobench.yml").targets["fast"]
    assert target.harness == "codex"
    assert target.model == "gpt-5.3-codex"
    assert target.command is None


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


# ------------------------------------------------- build exit codes (issue #35)


def _failing_verifier_config(repo: Path) -> None:
    """A verifier that always fails decisively (exit 1): every candidate must
    die in validation, so `benchmark build` produces zero tasks."""
    cfg = RepoBenchConfig()
    fail_cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    cfg.project = ProjectConfig(
        language="python",
        test_command=fail_cmd,
        regression_command=fail_cmd,
    )
    cfg.save(repo / "repobench.yml")


def _hermetic_verifier_config(repo: Path) -> None:
    """Point the verifier at the running interpreter's pytest (as the e2e suite
    does) so validation passes without depending on a PATH pytest."""
    cfg = RepoBenchConfig()
    pytest_cmd = f'"{sys.executable}" -m pytest -q'
    cfg.project = ProjectConfig(
        language="python",
        test_command=pytest_cmd,
        regression_command=pytest_cmd,
    )
    cfg.save(repo / "repobench.yml")


def test_benchmark_build_with_zero_valid_tasks_exits_one(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #35 regression: a build that produces nothing must fail loudly —
    scripts can only detect failure through the exit code."""
    monkeypatch.chdir(fixture_repo)
    _failing_verifier_config(fixture_repo)
    assert _invoke("analyze").exit_code == 0
    built = _invoke("benchmark", "build")
    assert built.exit_code == 1, built.output
    assert "error: no valid tasks were produced" in built.output
    # the failure points at the per-check diagnostics surface
    assert "candidates --show" in built.output
    storage = Storage(fixture_repo / ".repobench" / "state.db")
    assert storage.list_benchmarks() == []  # nothing was frozen


def test_benchmark_build_before_analyze_exits_one(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    built = _invoke("benchmark", "build")
    assert built.exit_code == 1, built.output
    assert "no task candidates to validate" in built.output


def test_benchmark_build_logs_cost_warning_and_candidate_progress(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persona test: a build must say how much work is coming and announce
    each candidate BEFORE spending minutes validating it."""
    monkeypatch.chdir(fixture_repo)
    _hermetic_verifier_config(fixture_repo)
    assert _invoke("analyze").exit_code == 0
    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    # cost warning before the loop (fixture pool: PR #7 only — PR #8 is FILTERED)
    assert "validating 1 candidate(s)" in built.output
    assert "this is the slow step" in built.output
    # 1-based per-candidate progress line, emitted before the candidate's work
    assert "[1/1] PR #7: validating…" in built.output


def test_benchmark_refresh_without_benchmark_exits_one(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`benchmark refresh` routes through the same UsageError → exit 1 path."""
    monkeypatch.chdir(fixture_repo)
    _write_config(fixture_repo)
    assert _invoke("analyze").exit_code == 0
    refreshed = _invoke("benchmark", "refresh")
    assert refreshed.exit_code == 1, refreshed.output
    assert "no benchmark found" in refreshed.output


# --------------------------------------------- candidates --show (issue #35)


def test_candidates_show_prints_per_check_validation(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    _hermetic_verifier_config(fixture_repo)
    assert _invoke("analyze").exit_code == 0
    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output

    storage = Storage(fixture_repo / ".repobench" / "state.db")
    task_rows = storage.tasks_for_pr(7)
    assert len(task_rows) == 1
    shown = _invoke("candidates", "--show", "7")
    assert shown.exit_code == 0, shown.output
    assert task_rows[0]["task_id"] in shown.output
    assert "VALID" in shown.output
    # every recorded check is surfaced with its outcome — no sqlite spelunking
    for row in storage.validation_history(task_rows[0]["task_id"]):
        assert row["kind"] in shown.output
        assert row["result"] in shown.output


def test_candidates_show_prints_failed_checks_after_rejected_build(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    _failing_verifier_config(fixture_repo)
    assert _invoke("analyze").exit_code == 0
    assert _invoke("benchmark", "build").exit_code == 1
    shown = _invoke("candidates", "--show", "7")
    assert shown.exit_code == 0, shown.output
    assert "REJECTED" in shown.output
    assert "BASELINE_BROKEN" in shown.output
    assert "failed" in shown.output


def test_candidates_show_filtered_pr_shows_mining_rejection(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    assert _invoke("analyze").exit_code == 0
    shown = _invoke("candidates", "--show", "8")  # filtered before packaging
    assert shown.exit_code == 0, shown.output
    assert "FILTERED" in shown.output
    assert "NO_TEST_CHANGE" in shown.output
    assert Storage(fixture_repo / ".repobench" / "state.db").tasks_for_pr(8) == []


def test_candidates_show_unknown_pr_is_a_usage_error(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    assert _invoke("analyze").exit_code == 0
    shown = _invoke("candidates", "--show", "42")
    assert shown.exit_code == 1, shown.output
    assert "no candidate recorded for PR #42" in shown.output


# ------------------------------------------------- build early-stop (unit test)


def test_build_benchmark_stop_when_sufficient_stops_after_enough_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--stop-when-sufficient (opt-in): the validation loop breaks as soon as
    enough VALID tasks cover the requested size; the default validates all."""
    import tarfile

    from repobench.cli import builds
    from repobench.cli.builds import build_benchmark
    from repobench.core.types import (
        Assessment,
        CandidateInfo,
        PRInfo,
        TaskMetadata,
        TaskPackage,
        TaskStatus,
        TaskType,
    )
    from repobench.validation._shared import CheckResult
    from repobench.validation.pipeline import TaskValidationReport

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # GitRepo only needs the .git directory (nothing is mined here; the
    # synthetic candidates below are persisted straight into storage).
    git(repo_root, "init", "--quiet", "--initial-branch=main")

    def candidate(n: int) -> CandidateInfo:
        return CandidateInfo(
            candidate_id=f"c{n}",
            pr=PRInfo(number=n, title=f"PR {n}"),
            assessment=Assessment(
                task_type=TaskType.BUGFIX, subsystem=f"mod{n}"
            ),
        )

    storage = Storage(tmp_path / "state.db")
    for n in (1, 2, 3):
        storage.save_candidate(candidate(n))

    packages_root = tmp_path / "packages"

    def fake_package_for(repo, cand, paths):  # noqa: ANN001 - test double
        directory = packages_root / cand.candidate_id
        directory.mkdir(parents=True, exist_ok=True)
        with tarfile.open(directory / "base.tar", "w"):
            pass  # empty archive — the leakage scan finds nothing, score clears
        metadata = TaskMetadata(
            task_id=f"task-{cand.pr.number}",
            base_sha="b" * 40,
            gold_sha="g" * 40,
            pr_number=cand.pr.number,
            assessment=cand.assessment,
        )
        return TaskPackage(task_id=metadata.task_id, directory=directory, metadata=metadata)

    calls: list[str] = []

    class FakeValidator:
        def __init__(self, project, workspaces_root) -> None:  # noqa: ANN001
            pass

        def validate(self, package, *, leakages=None):  # noqa: ANN001
            calls.append(package.task_id)
            return TaskValidationReport(
                task_id=package.task_id,
                status=TaskStatus.VALID,
                rejection_code=None,
                checks=[CheckResult(name="fake", passed=True, code=None, details="ok")],
            )

    monkeypatch.setattr(builds, "_package_for", fake_package_for)
    monkeypatch.setattr(builds, "TaskValidator", FakeValidator)

    cfg = RepoBenchConfig()
    cfg.project = ProjectConfig(
        language="python", test_command="true", regression_command="true"
    )

    # early stop ON: one VALID task covers size 1 — candidates 2 and 3 never run
    calls.clear()
    lines: list[str] = []
    outcome = build_benchmark(
        repo_root, cfg, storage, size=1, stop_when_sufficient=True, log=lines.append
    )
    assert calls == ["task-1"]
    assert len(outcome.valid) == 1
    assert len(outcome.sample) == 1
    stopping = [line for line in lines if "stopping early" in line]
    assert len(stopping) == 1
    assert "1 valid task(s) cover size 1" in stopping[0]
    assert "2 candidate(s) left unvalidated" in stopping[0]

    # default OFF: every candidate is validated (current methodology untouched)
    calls.clear()
    lines_default: list[str] = []
    outcome_all = build_benchmark(repo_root, cfg, storage, size=1, log=lines_default.append)
    assert calls == ["task-1", "task-2", "task-3"]
    assert len(outcome_all.valid) == 3
    assert not any("stopping early" in line for line in lines_default)
