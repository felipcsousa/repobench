"""End-to-end tests (PRD §121-127, §139-141): fixture repository → init → analyze
→ benchmark build (real pytest validation) → run with fake command targets →
report, plus the adversarial leakage check on the synthetic workspace."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repobench.cli.app import app
from repobench.config import ProjectConfig, RepoBenchConfig
from repobench.core.types import ExecutionTarget, TrialOutcome
from repobench.storage.db import Storage

runner = CliRunner()

SYNTHETIC_BASE_SUBJECT = "RepoBench benchmark base"


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _configure_targets(repo: Path, fake_agent_path: Path, **targets: ExecutionTarget) -> None:
    """Simulate the user editing repobench.yml: point the verifier commands at the
    local venv's pytest (hermetic — no install step, no network) and add fake
    command targets (PRD §25)."""
    cfg = RepoBenchConfig.load(repo / "repobench.yml")
    pytest_cmd = f'"{sys.executable}" -m pytest -q'
    cfg.project = ProjectConfig(
        language="python",
        test_command=pytest_cmd,
        regression_command=pytest_cmd,
    )
    cfg.targets.update(targets)
    cfg.save(repo / "repobench.yml")


def _command_target(name: str, fake_agent_path: Path, *extra: str) -> ExecutionTarget:
    return ExecutionTarget(
        id=name,
        harness="command",
        command=[sys.executable, str(fake_agent_path), "{workspace}", *extra],
    )


def _fast_forward(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Storage:
    """init → analyze → benchmark build through the CLI, leaving state on disk."""
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    _configure_targets(
        fixture_repo,
        fake_agent_path,
        fixer=_command_target("fixer", fake_agent_path),
        noop=_command_target("noop", fake_agent_path, "noop"),
    )
    analyzed = _invoke("analyze")
    assert analyzed.exit_code == 0, analyzed.output
    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    return Storage(fixture_repo / ".repobench" / "state.db")


# ------------------------------------------------------------------ golden path


def test_golden_path_init_analyze_build_run_report(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)

    # -- init ---------------------------------------------------------------
    initialized = _invoke("init", "--yes")
    assert initialized.exit_code == 0, initialized.output
    assert (fixture_repo / "repobench.yml").is_file()
    assert ".repobench/" in (fixture_repo / ".gitignore").read_text().split()

    # -- analyze (PRD §10: candidates without spending a token) -------------
    analyzed = _invoke("analyze")
    assert analyzed.exit_code == 0, analyzed.output
    storage = Storage(fixture_repo / ".repobench" / "state.db")
    candidates = {c.pr.number: c for c in storage.list_candidates()}
    discovered = [c for c in candidates.values() if c.status.value == "DISCOVERED"]
    assert len(discovered) >= 1
    assert candidates[8].status.value == "FILTERED"
    assert candidates[8].rejection_code.value == "NO_TEST_CHANGE"

    # -- benchmark build (PRD §88: real pytest validation runs here) --------
    _configure_targets(
        fixture_repo,
        fake_agent_path,
        fixer=_command_target("fixer", fake_agent_path),
        noop=_command_target("noop", fake_agent_path, "noop"),
    )
    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    assert "Benchmark" in built.output and "Health" in built.output
    benchmarks = storage.list_benchmarks()
    assert len(benchmarks) == 1
    benchmark_id = benchmarks[0]["benchmark_id"]
    task_ids = storage.benchmark_task_ids(benchmark_id)
    assert len(task_ids) == 1
    manifest = (
        fixture_repo / ".repobench" / "benchmarks" / benchmark_id / "manifest.json"
    )
    assert manifest.is_file()
    assert json.loads(manifest.read_text())["benchmark_id"] == benchmark_id
    assert storage.list_candidates()[0].status.value == "VALID"

    # -- run (PRD §96-99: preview + 2 trials with --yes) --------------------
    result = _invoke("run", "fixer", "noop", "--yes")
    assert result.exit_code == 0, result.output
    assert "Network isolation" in result.output and "none" in result.output
    assert "SOLVED" in result.output and "UNSOLVED" in result.output
    runs = storage.list_runs()
    assert len(runs) == 1
    run_id = runs[0]["run_id"]
    assert runs[0]["status"] == "COMPLETED"
    trials = {t.target_id: t for t in storage.list_trials(run_id)}
    assert len(trials) == 2
    assert trials["fixer"].outcome is TrialOutcome.SOLVED
    assert trials["noop"].outcome is TrialOutcome.UNSOLVED

    # -- report (PRD §111-112) ----------------------------------------------
    report_json = _invoke("report", "--format", "json")
    assert report_json.exit_code == 0, report_json.output
    data = json.loads(report_json.output)
    rates = {t["target_id"]: t["solve_rate"] for t in data["targets"]}
    assert rates == {"fixer": 1.0, "noop": 0.0}
    assert data["benchmark_id"] == benchmark_id
    assert data["run_id"] == run_id

    report_text = _invoke("report")
    assert report_text.exit_code == 0, report_text.output
    assert "95% CI" in report_text.output or "No conclusive" in report_text.output

    report_html = _invoke("report", "--format", "html")
    assert report_html.exit_code == 0
    assert "P1" in report_html.output


def test_run_requires_benchmark_and_known_targets(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)

    missing_benchmark = runner.invoke(app, ["run", "fixer", "--yes", "--benchmark", "rb_b_nope"])
    assert missing_benchmark.exit_code == 1
    assert "unknown benchmark" in missing_benchmark.output

    unknown_target = _invoke("run", "claude-default", "--yes")
    assert unknown_target.exit_code == 1
    assert "unknown target" in unknown_target.output
    assert "fixer" in unknown_target.output  # available ids listed

    no_targets = _invoke("run")
    assert no_targets.exit_code == 1
    assert "no targets specified" in no_targets.output


def test_run_rejects_multiple_rollouts(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
    result = _invoke("run", "fixer", "--yes", "--rollouts", "3")
    assert result.exit_code == 1
    assert "V1.5" in result.output
    assert "PRD §103" in result.output


def test_run_resume_skips_completed_trials(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
    storage = Storage(fixture_repo / ".repobench" / "state.db")

    first = _invoke("run", "fixer", "--yes")
    assert first.exit_code == 0, first.output
    trials_after_first = storage.list_trials()
    assert len(trials_after_first) == 1

    # Resume re-runs nothing for the same target; adding a target executes only
    # the missing Task×Target pair under the same benchmark.
    second = _invoke("run", "fixer", "noop", "--yes", "--resume")
    assert second.exit_code == 0, second.output
    assert "resuming the latest run" in second.output
    assert "Already complete" in second.output
    all_trials = storage.list_trials()
    assert len(all_trials) == 2
    by_target = {t.target_id: t.outcome for t in all_trials}
    assert by_target["fixer"] is TrialOutcome.SOLVED
    assert by_target["noop"] is TrialOutcome.UNSOLVED
    # resuming kept the original run identity — no second run row was created
    runs = storage.list_runs()
    assert len(runs) == 1
    resumed_trials = storage.list_trials(runs[0]["run_id"])
    assert {t.target_id for t in resumed_trials} == {"fixer", "noop"}
    assert runs[0]["status"] == "COMPLETED"


# ------------------------------------------------------------- adversarial (PRD §139)


def test_adversarial_leaker_finds_no_solution_in_workspace(
    fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cheating agent runs `git log`, `git remote -v` and dumps TOKEN env vars
    inside the trial workspace — the synthetic repo must give it nothing (PRD §125)."""
    _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
    _configure_targets(
        fixture_repo,
        fake_agent_path,
        leaker=_command_target("leaker", fake_agent_path, "leaker"),
    )
    # Seed credentials so the sanitization is actually exercised.
    monkeypatch.setenv("GH_TOKEN", "ghp_fixture_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fixture_secret")

    result = _invoke("run", "leaker", "--yes", "--keep-workspaces")
    assert result.exit_code == 0, result.output
    storage = Storage(fixture_repo / ".repobench" / "state.db")
    trials = [t for t in storage.list_trials() if t.target_id == "leaker"]
    assert len(trials) == 1
    assert trials[0].outcome is TrialOutcome.SOLVED  # the fake agent also fixes the bug

    kept = fixture_repo / ".repobench" / "workspaces" / trials[0].trial_id / "repo"
    assert kept.is_dir(), "workspace must be kept with --keep-workspaces"

    # exactly one synthetic commit, no history from the fixture repository
    subjects = _git(kept, "log", "--format=%s").splitlines()
    assert subjects == [SYNTHETIC_BASE_SUBJECT]
    assert "Merge pull request #7" not in _git(kept, "log", "--format=%H %s")
    # no remotes configured
    assert _git(kept, "remote", "-v") == ""
    # no gold/verifier patches inside the workspace
    assert not (kept / "gold.patch").exists()
    assert not (kept / "verifier.patch").exists()
    assert "x % 2 == 0" in (kept / "calculator.py").read_text()  # the fix happened
    assert not (kept / "tests" / "test_sum_even.py").exists()  # hidden verifier absent

    # the token dump the agent collected inside the trial
    leak_report = (kept / "leak_report.txt").read_text()
    assert SYNTHETIC_BASE_SUBJECT in leak_report  # git log saw only the synthetic commit
    assert "origin" not in leak_report  # git remote -v was empty
    token_lines = leak_report.splitlines()[leak_report.splitlines().index("token env keys:") + 1:]
    assert "GH_TOKEN" not in token_lines
    assert "GITHUB_TOKEN" not in token_lines
