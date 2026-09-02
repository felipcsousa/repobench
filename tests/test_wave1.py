"""Onda 1 quick wins (issues #1-#12): everything wired on top of the existing core.

Unit tests for the new modules plus CLI-level tests driving the same fast-forward
path as the e2e suite (init → analyze → benchmark build → run with fake agents).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repobench.analysis.metrics import segment_breakdown
from repobench.benchmark.health import PUBLIC_REPOSITORY_WARNING, compute_health
from repobench.config import RepoBenchConfig
from repobench.core.types import (
    Assessment,
    ExecutionTarget,
    TaskMetadata,
    TrialOutcome,
    TrialResult,
)
from repobench.execution.fingerprint import (
    build_run_manifest,
    config_hash,
    instruction_file_hashes,
    target_fingerprint,
)
from repobench.mining.subsystem import detect_package_dirs, load_codeowners, parse_codeowners
from repobench.reporting.export import CSV_COLUMNS, render_csv, render_jsonl
from repobench.storage.db import Storage
from tests.test_e2e import _command_target, _configure_targets, _fast_forward, _invoke


def _run_completed(storage: Storage, repo: Path, *, target_id: str = "fixer") -> str:
    """The single run id recorded by a completed fast-forward run."""
    runs = storage.list_runs()
    assert runs and runs[0]["status"] == "COMPLETED"
    run_id = runs[0]["run_id"]
    assert (repo / ".repobench" / "runs" / run_id).is_dir()
    return run_id


# ------------------------------------------------------------- #2 fingerprint


class TestFingerprint:
    def test_target_fingerprint_excludes_env_values(self) -> None:
        target = ExecutionTarget(
            id="glm",
            harness="opencode",
            model="zai/glm-x",
            env={"API_TOKEN": "hunter2", "OTHER": "x"},
        )
        fp = target_fingerprint(target)
        blob = json.dumps(fp)
        assert "hunter2" not in blob  # credential contents never persist (PRD §29)
        assert fp["definition"]["env_keys"] == ["API_TOKEN", "OTHER"]

    def test_config_hash_is_stable_and_input_sensitive(self) -> None:
        a = config_hash({"args": ["-x"], "model": "m"})
        b = config_hash({"model": "m", "args": ["-x"]})  # key order irrelevant
        assert a == b
        assert a != config_hash({"args": ["-y"], "model": "m"})

    def test_instruction_file_hashes_only_known_files(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("be kind")
        (tmp_path / "CLAUDE.md").write_text("be brief")
        (tmp_path / "secrets.env").write_text("TOKEN=1")
        hashes = instruction_file_hashes(tmp_path)
        assert set(hashes) == {"AGENTS.md", "CLAUDE.md"}
        assert all(value.startswith("sha256:") for value in hashes.values())

    def test_build_run_manifest_reproducibility_record(self) -> None:
        target = ExecutionTarget(id="glm", harness="command", command=["echo", "{workspace}"])
        manifest = build_run_manifest(
            run_id="run_x",
            benchmark_id="rb_b_x",
            targets=[target],
            harness_versions={"command": "1.2.3"},
            instruction_hashes={"AGENTS.md": "sha256:abc"},
            bootstrap_seed=42,
            started_at="2026-09-01T00:00:00+00:00",
            repobench_version="0.4.0",
        )
        assert manifest["bootstrap_seed"] == 42
        assert manifest["harnesses"] == {"command": "1.2.3"}
        assert manifest["targets"][0]["harness_version"] == "1.2.3"
        assert manifest["targets"][0]["config_hash"].startswith("sha256:")
        # PRD §30 fields
        for field in ("repobench_version", "os", "arch", "python_version"):
            assert manifest[field]


# ------------------------------------------------------------- #6 CODEOWNERS


class TestCodeowners:
    def test_parse_skips_comments_and_keeps_first_owner(self) -> None:
        owners = parse_codeowners(
            "# comment\n\n/src/payments/ @payments @backup\n*.md docs@example.com\n"
        )
        # prefixes are stored stripped — matching normalizes both sides
        assert owners == {"src/payments": "payments", "*.md": "docs@example.com"}

    def test_parse_later_rule_wins(self) -> None:
        owners = parse_codeowners("src/ @a\nsrc/ @b\n")
        assert owners["src"] == "b"

    def test_load_codeowners_root_and_github_locations(self, tmp_path: Path) -> None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text("src/ @gh-team\n")
        assert load_codeowners(tmp_path) == {"src": "gh-team"}
        (tmp_path / "CODEOWNERS").write_text("src/ @root-team\n")  # root takes precedence
        assert load_codeowners(tmp_path) == {"src": "root-team"}

    def test_detect_package_dirs_python_src_and_js(self, tmp_path: Path) -> None:
        (tmp_path / "pkg1").mkdir()
        (tmp_path / "pkg1" / "__init__.py").write_text("")
        (tmp_path / "src" / "pkg2").mkdir(parents=True)
        (tmp_path / "src" / "pkg2" / "__init__.py").write_text("")
        (tmp_path / "packages" / "ui").mkdir(parents=True)
        (tmp_path / "packages" / "ui" / "package.json").write_text("{}")
        (tmp_path / "docs").mkdir()  # not a package
        assert detect_package_dirs(tmp_path) == {
            "pkg1": "pkg1",
            "src/pkg2": "pkg2",
            "packages/ui": "ui",
        }

    def test_analyze_uses_codeowners_for_subsystem(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (fixture_repo / ".github").mkdir()
        (fixture_repo / ".github" / "CODEOWNERS").write_text("calculator.py @calc-team\n")
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)  # init + analyze + build
        storage = Storage(fixture_repo / ".repobench" / "state.db")
        by_pr = {c.pr.number: c for c in storage.list_candidates()}
        assert by_pr[7].assessment.subsystem == "calc-team"


# ---------------------------------------------------- #12 instruction segments


class TestInstructionSegments:
    def test_segment_breakdown_supports_instruction_confidence(self) -> None:
        trial = TrialResult(
            trial_id="t1", task_id="task_d", target_id="glm", outcome=TrialOutcome.SOLVED
        )
        task = TaskMetadata(
            task_id="task_d",
            base_sha="b" * 40,
            gold_sha="g" * 40,
            assessment=Assessment(instruction_confidence="D"),
        )
        segments = segment_breakdown([trial], {"task_d": task}, "instruction_confidence")
        assert segments["glm"]["D"].rate == 1.0

    def test_unknown_dimension_still_rejected(self) -> None:
        with pytest.raises(ValueError):
            segment_breakdown([], {}, "nope")


# ----------------------------------------------------------------- #5 export


def _sample_trials() -> tuple[list[TrialResult], dict[str, TaskMetadata]]:
    trial = TrialResult(
        trial_id="trial_1",
        run_id="run_1",
        benchmark_id="rb_b_1",
        task_id="task_1",
        target_id="glm",
        harness="command",
        harness_version="1.0",
        outcome=TrialOutcome.SOLVED,
        duration_ms=1500,
        usage=None,
        cost_usd=0.1,
        cost_source="USER_PROVIDED_PRICING",
        files_changed=2,
    )
    task = TaskMetadata(
        task_id="task_1",
        pr_number=7,
        base_sha="b" * 40,
        gold_sha="g" * 40,
        assessment=Assessment(
            instruction_confidence="C", subsystem="calc", implementation_loc=10
        ),
    )
    return [trial], {"task_1": task}


class TestTrialExport:
    def test_export_row_covers_csv_columns_exactly(self) -> None:
        # Drift guard: the explicit row builder and the CSV header are one contract.
        from repobench.reporting.export import _export_row

        trials, tasks = _sample_trials()
        assert set(_export_row(trials[0], tasks["task_1"])) == set(CSV_COLUMNS)
        # a trial without task metadata leaves the task.* columns absent, not wrong
        assert set(_export_row(trials[0], None)) == set(CSV_COLUMNS) - {
            c for c in CSV_COLUMNS if c.startswith("task.")
        }

    def test_jsonl_round_trips_to_trial_result(self) -> None:
        trials, tasks = _sample_trials()
        lines = render_jsonl(trials, tasks).splitlines()
        assert len(lines) == 1
        parsed = TrialResult.model_validate_json(lines[0])
        assert parsed == trials[0]
        joined = json.loads(lines[0])["task"]
        assert joined["pr_number"] == 7 and joined["instruction_confidence"] == "C"

    def test_csv_header_and_row(self) -> None:
        trials, tasks = _sample_trials()
        rows = render_csv(trials, tasks).splitlines()
        assert rows[0].split(",")[:6] == list(CSV_COLUMNS[:6])
        assert "trial_1" in rows[1]
        assert "SOLVED" in rows[1]
        assert "task_1" in rows[1]
        # every exported column appears in the header exactly once
        assert rows[0].count("target_id") == 1

    def test_missing_task_leaves_task_columns_empty(self) -> None:
        trials, _ = _sample_trials()
        row = render_csv(trials, None).splitlines()[1].split(",")
        header = list(CSV_COLUMNS)
        assert row[header.index("task.subsystem")] == ""
        assert row[header.index("outcome")] == "SOLVED"


# ------------------------------------------------------------ #8 public repo


class TestPublicRepository:
    def test_compute_health_appends_prd51_warning(self) -> None:
        from repobench.benchmark.coverage import CoverageReport

        coverage = CoverageReport(task_type=80, subsystem=80, complexity=80, overall=80)
        report = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=100,
            tasks=[],
            public_repository=True,
        )
        assert PUBLIC_REPOSITORY_WARNING in report.warnings
        clean = compute_health(
            coverage=coverage,
            all_checks_passed_ratio=1.0,
            leakage_score=100,
            tasks=[],
            public_repository=False,
        )
        assert PUBLIC_REPOSITORY_WARNING not in clean.warnings

    def test_analyze_renders_public_warning_block(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fixture_repo)
        assert _invoke("init", "--yes").exit_code == 0
        _configure_targets(
            fixture_repo, fake_agent_path, fixer=_command_target("fixer", fake_agent_path)
        )
        # A GitHub remote + stubbed gh client: enrich stays offline, visibility
        # reports PUBLIC — deterministic regardless of gh being installed.
        from tests.fixtures.gitutil import git

        git(fixture_repo, "remote", "add", "origin", "https://github.com/acme/calc-fixture.git")

        class StubClient:
            def __init__(self, slug: str) -> None:
                pass

            def enrich(self, pr):  # noqa: ANN001 - PRInfo
                return pr

            def merged_pr_count(self, since, limit=500):  # noqa: ANN001 - issue #31
                return None  # no ground truth in this stub — recall stays hidden

            def visibility(self) -> str:
                return "PUBLIC"

        monkeypatch.setattr("repobench.cli.services.GitHubClient", StubClient)
        result = _invoke("analyze")
        assert result.exit_code == 0, result.output
        assert "PUBLIC REPOSITORY" in result.output
        assert "contamination-free capability" in result.output

    def test_benchmark_health_carries_warning_into_report(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        # build_benchmark lives in cli.builds since wave 2 — patch its binding.
        monkeypatch.setattr(
            "repobench.cli.builds.repository_visibility", lambda slug: "PUBLIC"
        )
        # Rebuild so the public-repo flag lands inside the persisted health report.
        built = _invoke("benchmark", "build")
        assert built.exit_code == 0, built.output
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        report = _invoke("report", "--format", "json")
        data = json.loads(report.output)
        assert any("Public repository" in warning for warning in data["warnings"])


# ---------------------------------------------------------- #10 resume retry


class TestResumeRetry:
    def _plan_with_previous(
        self, storage: Storage, fixture_repo: Path, outcome: TrialOutcome, **plan_kwargs
    ):
        from repobench.cli.services import plan_run
        from repobench.core.paths import ProjectPaths

        benchmark_id = storage.list_benchmarks()[0]["benchmark_id"]
        task_id = storage.benchmark_task_ids(benchmark_id)[0]
        storage.create_run("run_prev", benchmark_id)
        storage.save_trial(
            TrialResult(
                trial_id="trial_prev",
                run_id="run_prev",
                benchmark_id=benchmark_id,
                task_id=task_id,
                target_id="fixer",
                outcome=outcome,
            )
        )
        cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
        target = ExecutionTarget(
            id="fixer", harness="command", command=["true", "{workspace}"]
        )
        return plan_run(
            storage,
            ProjectPaths(fixture_repo),
            cfg,
            targets=[target],
            resume=True,
            **plan_kwargs,
        )

    def test_timeout_is_retried_on_resume(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        plan = self._plan_with_previous(storage, fixture_repo, TrialOutcome.TIMEOUT)
        assert plan.retried == 1
        assert len(plan.pairs) == 1

    def test_unsolved_retried_only_with_flag(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        plan = self._plan_with_previous(storage, fixture_repo, TrialOutcome.UNSOLVED)
        assert plan.retried == 0 and plan.already_complete == 1
        plan = self._plan_with_previous(
            storage, fixture_repo, TrialOutcome.UNSOLVED, retry_failed=True
        )
        assert plan.retried == 1 and len(plan.pairs) == 1

    def test_solved_never_retried(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        plan = self._plan_with_previous(
            storage, fixture_repo, TrialOutcome.SOLVED, retry_failed=True
        )
        assert plan.retried == 0 and plan.already_complete == 1

    def test_trial_result_outcome_is_required(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            TrialResult(trial_id="t", task_id="task", target_id="glm")


# -------------------------------------------------- #2 harness version cache


class TestHarnessVersionCache:
    def test_version_probed_once_per_process(self) -> None:
        from repobench.execution.adapters.base import HarnessAdapter
        from repobench.execution import runner as runner_module

        calls = {"n": 0}

        class Stub(HarnessAdapter):
            name = "stub-versioned"

            def validate_target(self, target):  # pragma: no cover - unused here
                from repobench.execution.adapters.base import ValidationResult

                return ValidationResult()

            def build_command(self, *args, **kwargs):  # pragma: no cover
                raise NotImplementedError

            def version(self) -> str | None:
                calls["n"] += 1
                return "1.2.3"

        adapter = Stub()
        # Isolate from any earlier cache entry for this harness name.
        runner_module._HARNESS_VERSION_CACHE.pop("stub-versioned", None)
        assert runner_module.cached_harness_version(adapter) == "1.2.3"
        assert runner_module.cached_harness_version(adapter) == "1.2.3"
        assert calls["n"] == 1  # second lookup served from the cache


# --------------------------------------------------------------- #7 trust gate


class TestCustomCommandTrust:
    def test_first_execution_is_gated_then_trusted_after_registration(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        blocked = _invoke("run", "fixer", "--yes")
        assert blocked.exit_code == 1
        assert "--trust-custom-command" in blocked.output
        assert "fake_agent.py" in blocked.output  # the template is shown for review

        allowed = _invoke("run", "fixer", "--yes", "--trust-custom-command")
        assert allowed.exit_code == 0, allowed.output

        # Same template again needs no flag: registration is persisted trust.
        again = _invoke("run", "fixer", "--yes", "--resume")
        assert again.exit_code == 0, again.output
        assert "Already complete" in again.output

    def test_changed_template_is_gated_again(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        _configure_targets(
            fixture_repo,
            fake_agent_path,
            fixer=_command_target("fixer", fake_agent_path, "noop"),  # different template
        )
        blocked = _invoke("run", "fixer", "--yes", "--resume")
        assert blocked.exit_code == 1
        assert "--trust-custom-command" in blocked.output

    def test_persisted_config_trust_skips_gate(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
        cfg.execution.trust_custom_commands = True
        cfg.save(fixture_repo / "repobench.yml")
        result = _invoke("run", "fixer", "--yes")
        assert result.exit_code == 0, result.output

    def test_run_preview_shows_command_template(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        result = _invoke("run", "fixer", "--yes", "--trust-custom-command")
        assert result.exit_code == 0, result.output
        assert "command[fixer]" in result.output
        assert "{workspace}" in result.output


# ------------------------------------------------- #2/#3 run artifacts (e2e-ish)


class TestRunArtifacts:
    def test_run_writes_manifest_and_registers_targets_and_output_logs(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        # Repository instruction files are fingerprinted when present (PRD §31).
        (fixture_repo / "AGENTS.md").write_text("be pragmatic")
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0

        storage = Storage(fixture_repo / ".repobench" / "state.db")
        run_id = _run_completed(storage, fixture_repo)
        manifest_path = fixture_repo / ".repobench" / "runs" / run_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["run_id"] == run_id
        assert manifest["repobench_version"]
        # The manifest is written BEFORE the matrix runs, so versions must have
        # been pre-probed — "command" is present (None: no binary to probe).
        assert "command" in manifest["harnesses"]
        assert manifest["targets"][0]["definition"]["id"] == "fixer"
        assert manifest["targets"][0]["config_hash"].startswith("sha256:")
        assert manifest["instruction_file_hashes"] == {
            "AGENTS.md": manifest["instruction_file_hashes"]["AGENTS.md"]
        }
        assert list(manifest["instruction_file_hashes"]) == ["AGENTS.md"]

        registered = storage.get_target("fixer")
        assert registered is not None and registered["harness"] == "command"
        assert "{workspace}" in registered["command"]

        trials = storage.list_trials(run_id)
        assert trials  # command harnesses report no binary version — None is honest
        trial_dir = fixture_repo / ".repobench" / "runs" / run_id / "trials" / trials[0].trial_id
        assert (trial_dir / "stdout.log").is_file()
        assert (trial_dir / "stderr.log").is_file()
        on_disk = json.loads((trial_dir / "trial.json").read_text())
        assert on_disk["stdout_path"].endswith("stdout.log")
        assert on_disk["stderr_path"].endswith("stderr.log")

    def test_report_warns_on_mixed_harness_versions(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        run_id = storage.list_runs()[0]["run_id"]
        trial = storage.list_trials(run_id)[0]
        storage.save_trial(trial.model_copy(update={"harness_version": "1.0.0"}))
        storage.save_trial(
            trial.model_copy(
                update={"trial_id": f"{trial.trial_id}_x", "harness_version": "2.0.0"}
            )
        )
        report = _invoke("report", "--format", "json")
        data = json.loads(report.output)
        assert any("multiple harness versions" in warning for warning in data["warnings"])


# ------------------------------------------------------------ #11 seed/config


class TestSeedAndConfigCleanup:
    def test_dead_knobs_are_gone_but_old_yaml_still_loads(self, tmp_path: Path) -> None:
        legacy = tmp_path / "repobench.yml"
        legacy.write_text(
            "version: 1\n"
            "repository: {provider: github, lookback_days: 30}\n"
            "project: {build_command: make}\n"
            "task_mining: {max_test_loc: 100}\n"
            "benchmark: {include_confidence_c: true}\n"
            "execution: {environment: inherit}\n"
        )
        cfg = RepoBenchConfig.load(legacy)
        assert cfg.repository.lookback_days == 30
        assert not hasattr(cfg.project, "build_command")
        assert cfg.execution.trust_custom_commands is False

    def test_bootstrap_seed_persisted_in_run_and_report(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        cfg = RepoBenchConfig.load(fixture_repo / "repobench.yml")
        cfg.analysis.bootstrap_seed = 7
        cfg.save(fixture_repo / "repobench.yml")
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        run_row = storage.list_runs()[0]
        assert json.loads(run_row["config_json"])["bootstrap_seed"] == 7
        report = _invoke("report")
        assert "Bootstrap seed: 7" in report.output
        data = json.loads(_invoke("report", "--format", "json").output)
        assert data["bootstrap_seed"] == 7

    def test_analyze_label_matches_prd10(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        result = _invoke("analyze")
        assert "Validated eval candidates" in result.output


# ------------------------------------------------------- #4 runs / #9 clean


class TestCleanScope:
    def test_flag_combinations(self) -> None:
        from repobench.cli.maintenance import CleanScope

        # plain `clean`: conservative default, prune beyond the newest run
        assert CleanScope.from_flags() == CleanScope(keep_runs=1, workspaces=False, cache=False)
        # --runs N
        assert CleanScope.from_flags(3).keep_runs == 3
        # scoped cleans never touch runs implicitly
        assert CleanScope.from_flags(workspaces=True) == CleanScope(
            keep_runs=None, workspaces=True, cache=False
        )
        assert CleanScope.from_flags(cache=True).keep_runs is None
        # --all covers everything and drops every run unless narrowed
        assert CleanScope.from_flags(all_scope=True) == CleanScope(
            keep_runs=0, workspaces=True, cache=True
        )
        assert CleanScope.from_flags(2, all_scope=True).keep_runs == 2

    def test_negative_runs_rejected(self) -> None:
        from repobench.cli.maintenance import CleanScope
        from repobench.core.errors import UsageError

        with pytest.raises(UsageError):
            CleanScope.from_flags(-1)


class TestRunsAndClean:
    def _two_runs(self, fixture_repo: Path, fake_agent_path: Path, monkeypatch) -> Storage:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        assert _invoke("run", "fixer", "--yes").exit_code == 0  # registered → no flag needed
        return storage

    def test_runs_lists_and_shows(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._two_runs(fixture_repo, fake_agent_path, monkeypatch)
        listing = _invoke("runs")
        assert listing.exit_code == 0, listing.output
        for run in storage.list_runs():
            assert run["run_id"] in listing.output
        assert "COMPLETED" in listing.output

        run_id = storage.list_runs()[0]["run_id"]
        shown = _invoke("runs", "--show", run_id)
        assert shown.exit_code == 0, shown.output
        assert "fixer" in shown.output and "SOLVED" in shown.output
        assert _invoke("runs", "--show", "run_nope").exit_code == 1

    def test_runs_reports_interrupted_status(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        storage.create_run("run_interrupted", storage.list_benchmarks()[0]["benchmark_id"])
        listing = _invoke("runs")
        assert "run_interrupted" in listing.output
        assert "RUNNING" in listing.output

    def test_clean_dry_run_by_default_then_apply(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._two_runs(fixture_repo, fake_agent_path, monkeypatch)
        newest, oldest = (r["run_id"] for r in storage.list_runs()[:2])
        (fixture_repo / ".repobench" / "workspaces" / "ws_leftover").mkdir(parents=True)
        (fixture_repo / ".repobench" / "cache").mkdir(parents=True)

        dry = _invoke("clean", "--all")
        assert dry.exit_code == 0, dry.output
        assert "would remove" in dry.output or "dry-run" in dry.output
        assert oldest in dry.output and newest in dry.output  # --all drops every run
        # dry-run removed nothing
        assert (fixture_repo / ".repobench" / "runs" / oldest).exists()

        applied = _invoke("clean", "--all", "--apply")
        assert applied.exit_code == 0, applied.output
        assert not (fixture_repo / ".repobench" / "runs" / oldest).exists()
        assert not (fixture_repo / ".repobench" / "runs" / newest).exists()
        assert not (fixture_repo / ".repobench" / "workspaces" / "ws_leftover").exists()
        assert not (fixture_repo / ".repobench" / "cache").exists()
        assert storage.list_runs() == []
        assert storage.list_trials() == []

    def test_clean_runs_n_keeps_newest(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._two_runs(fixture_repo, fake_agent_path, monkeypatch)
        newest, oldest = (r["run_id"] for r in storage.list_runs()[:2])
        applied = _invoke("clean", "--runs", "1", "--apply")
        assert applied.exit_code == 0, applied.output
        assert {r["run_id"] for r in storage.list_runs()} == {newest}
        assert not (fixture_repo / ".repobench" / "runs" / oldest).exists()

    def test_clean_workspaces_scope_never_touches_runs(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._two_runs(fixture_repo, fake_agent_path, monkeypatch)
        newest, oldest = (r["run_id"] for r in storage.list_runs()[:2])
        leftover = fixture_repo / ".repobench" / "workspaces" / "ws_leftover"
        leftover.mkdir(parents=True)
        applied = _invoke("clean", "--workspaces", "--apply")
        assert applied.exit_code == 0, applied.output
        assert not leftover.exists()
        # both runs (rows AND artifacts) survive a workspace-scoped clean
        assert {r["run_id"] for r in storage.list_runs()} == {newest, oldest}
        assert (fixture_repo / ".repobench" / "runs" / newest).is_dir()
        assert (fixture_repo / ".repobench" / "runs" / oldest).is_dir()

    def test_clean_empty_tree_is_a_noop(
        self, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(fixture_repo)
        assert _invoke("init", "--yes").exit_code == 0
        result = _invoke("clean", "--all")
        assert result.exit_code == 0
        assert "nothing to clean" in result.output


# ----------------------------------------------------- #1/#5/#12 report wiring


class TestReportWiring:
    def test_report_carries_pareto_tier_segments_and_export(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        _configure_targets(
            fixture_repo,
            fake_agent_path,
            fixer=_command_target("fixer", fake_agent_path),
            noop=_command_target("noop", fake_agent_path, "noop"),
        )
        assert (
            _invoke("run", "fixer", "noop", "--yes", "--trust-custom-command").exit_code == 0
        )

        # #1 Pareto: the report renders the plot; the best-quality target is
        # always on the frontier (the second one depends on harness durations).
        text = _invoke("report")
        assert "Pareto frontier — quality" in text.output
        assert "Frontier:" in text.output
        data = json.loads(_invoke("report", "--format", "json").output)
        assert data["pareto"]["axes"] in ("quality-cost", "quality-time")
        assert "fixer" in data["pareto"]["frontier"]

        # #12 instruction tier segment comes from task metadata
        assert "Segments — instruction_confidence" in text.output

        # #5 trial-level export round-trips
        jsonl = _invoke("report", "--format", "jsonl")
        assert jsonl.exit_code == 0, jsonl.output
        lines = [line for line in jsonl.output.splitlines() if line.strip()]
        assert len(lines) == 2
        for line in lines:
            parsed = TrialResult.model_validate_json(line)
            assert parsed.outcome in (TrialOutcome.SOLVED, TrialOutcome.UNSOLVED)

        csv_out = _invoke("report", "--format", "csv")
        assert csv_out.exit_code == 0, csv_out.output
        rows = csv_out.output.splitlines()
        assert rows[0].startswith("trial_id,run_id,benchmark_id")
        assert len(rows) == 3
        assert "instruction_confidence" in rows[0]

    def test_instruction_generation_stats_surface_in_report(
        self, fixture_repo: Path, fake_agent_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = _fast_forward(fixture_repo, fake_agent_path, monkeypatch)
        assert _invoke("run", "fixer", "--yes", "--trust-custom-command").exit_code == 0
        benchmark_id = storage.list_benchmarks()[0]["benchmark_id"]
        task_id = storage.benchmark_task_ids(benchmark_id)[0]
        # Simulate build-time generation extras on the task package metadata.
        metadata_path = fixture_repo / ".repobench" / "tasks" / task_id / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["generation"] = {"model": "glm", "attempts": 1}
        metadata_path.write_text(json.dumps(metadata))

        data = json.loads(_invoke("report", "--format", "json").output)
        assert data["instruction_generation"] == {"generated": 1, "failed": 0}
        text = _invoke("report")
        assert "Instruction generation: 1 generated · 0 fallback" in text.output
