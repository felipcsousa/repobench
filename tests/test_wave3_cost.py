"""Wave 3 cost credibility (issue #17): the bundled pricing catalog, the full
resolve_cost precedence chain (harness-reported > user pricing > catalog
estimate > unknown), the runner wiring of catalog estimates, unpriced-model
report warnings and the targets-list PRICING column."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repobench.cli.app import app
from repobench.cli.reports import build_report_data
from repobench.cli.render import _target_pricing_label
from repobench.analysis.metrics import TargetMetrics, aggregate_trials
from repobench.analysis.stats import wilson_ci
from repobench.config import ExecutionConfig, PricingRule, ProjectConfig, RepoBenchConfig
from repobench.core.types import (
    CommandSpec,
    ExecutionTarget,
    TrialOutcome,
    TrialResult,
    UsageRecord,
)
from repobench.execution.adapters.base import (
    HarnessAdapter,
    HarnessResult,
    ValidationResult,
)
from repobench.execution.pricing_catalog import CATALOG, CATALOG_VERSION, CatalogPrice, lookup
from repobench.execution.runner import TrialExecutor
from repobench.execution.usage import resolve_cost
from repobench.execution.workspace import WorkspaceManager
from repobench.reporting.models import ReportData
from repobench.reporting.terminal import render_report
from repobench.storage.db import Storage

from tests.test_runner import _make_task

RUN_ID = "run_w3"


# ---------------------------------------------------------------- resolve_cost


def test_resolve_cost_precedence_chain() -> None:
    """issue #17: harness-reported > user pricing > catalog estimate > unknown."""
    usage = UsageRecord(input_tokens=1_000_000, output_tokens=1_000_000)
    user_rule = PricingRule(input_per_million=1.0, output_per_million=1.0)
    catalog_entry = CatalogPrice(input_per_million=2.0, output_per_million=2.0)

    # 1. harness-reported wins over everything
    reported = UsageRecord(input_tokens=999_999, reported_cost_usd=0.5)
    assert resolve_cost(reported, user_rule, catalog_price=catalog_entry) == (
        0.5,
        "HARNESS_REPORTED",
    )

    # 2. the user's pricing rule beats the catalog
    assert resolve_cost(usage, user_rule, catalog_price=catalog_entry) == (
        pytest.approx(2.0),
        "USER_PROVIDED_PRICING",
    )

    # 3. the catalog estimate applies only when no user rule exists
    assert resolve_cost(usage, None, catalog_price=catalog_entry) == (
        pytest.approx(4.0),
        "CATALOG_ESTIMATE",
    )

    # 4. unknown when nothing applies — never invented
    assert resolve_cost(usage, None) == (None, None)
    assert resolve_cost(None, user_rule, catalog_price=catalog_entry) == (None, None)
    # usage without any token data: never a 0.0 cost, not even from the catalog
    assert resolve_cost(UsageRecord(), None, catalog_price=catalog_entry) == (None, None)


# --------------------------------------------------------------------- catalog


def test_catalog_entries_are_labeled_estimates_with_source() -> None:
    assert CATALOG, "catalog snapshot must not be empty"
    for key, price in CATALOG.items():
        assert price.estimate is True, key
        assert price.source == "catalog", key
        assert price.input_per_million > 0 and price.output_per_million > 0, key
    assert CATALOG_VERSION  # dated snapshot


def test_lookup_exact_and_case_insensitive() -> None:
    assert lookup("glm-4.6") is CATALOG["glm-4.6"]
    assert lookup("GLM-4.6") is CATALOG["glm-4.6"]
    assert lookup("  GPT-5.1  ") is CATALOG["gpt-5.1"]


def test_lookup_matches_dated_variant_prefix() -> None:
    assert lookup("claude-sonnet-4-5-20250929") is CATALOG["claude-sonnet-4-5"]


def test_lookup_matches_final_path_segment() -> None:
    # issue #17 rule: vendor prefixes are matched on the model's final "/" segment
    assert lookup("zai/glm-4.6") is CATALOG["glm-4.6"]
    assert lookup("openrouter/gpt-5.1") is CATALOG["gpt-5.1"]


def test_lookup_longest_prefix_wins() -> None:
    assert lookup("gpt-5.1-codex") is CATALOG["gpt-5.1-codex"]


def test_lookup_unknown_model_returns_none() -> None:
    # a vendor-prefixed model never matches an unlisted family name
    assert lookup("openrouter/minimax-x") is None
    assert lookup("totally-bogus-model") is None
    assert lookup("") is None
    assert lookup("   ") is None
    assert lookup(None) is None


# ------------------------------------------------- runner catalog integration


class _TokenReportingAdapter(HarnessAdapter):
    """Stub standing in for a harness that reports tokens but no cost."""

    name = "command"
    binary = sys.executable

    def validate_target(self, target: ExecutionTarget) -> ValidationResult:
        return ValidationResult()

    def build_command(
        self,
        target: ExecutionTarget,
        prompt: str,
        workspace: Path,
        *,
        task_id: str = "",
        target_id: str = "",
        timeout_seconds: int = 1200,
    ) -> CommandSpec:
        return CommandSpec(
            argv=[sys.executable, "-c", "pass"], cwd=workspace, timeout_seconds=timeout_seconds
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        return HarnessResult(usage=UsageRecord(input_tokens=1_000_000, output_tokens=1_000_000))


def _stub_executor(tmp_path: Path, pricing: dict[str, PricingRule] | None) -> TrialExecutor:
    return TrialExecutor(
        workspaces=WorkspaceManager(tmp_path / "workspaces"),
        execution_cfg=ExecutionConfig(),
        # always-failing verifier keeps these trials hermetic and quick
        project_cfg=ProjectConfig(test_command=f'"{sys.executable}" -c "import sys; sys.exit(1)"'),
        pricing=pricing,
        artifacts_dir=tmp_path / "artifacts",
        adapter_lookup=lambda harness: _TokenReportingAdapter(),
    )


async def test_runner_catalog_estimate_when_model_unpriced(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = _stub_executor(tmp_path, pricing=None)
    target = ExecutionTarget(id="glm", harness="command", model="glm-4.6")

    result = await executor.execute(task, target)

    assert result.usage is not None
    assert result.cost_source == "CATALOG_ESTIMATE"
    assert result.cost_usd == pytest.approx(0.60 + 2.20)  # glm-4.6 catalog prices


async def test_runner_user_pricing_beats_catalog(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = _stub_executor(
        tmp_path,
        pricing={"glm-4.6": PricingRule(input_per_million=1.0, output_per_million=1.0)},
    )
    target = ExecutionTarget(id="glm", harness="command", model="glm-4.6")

    result = await executor.execute(task, target)

    assert result.cost_source == "USER_PROVIDED_PRICING"
    assert result.cost_usd == pytest.approx(2.0)


async def test_runner_unknown_model_with_tokens_stays_unpriced(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = _stub_executor(tmp_path, pricing=None)
    target = ExecutionTarget(id="odd", harness="command", model="openrouter/minimax-x")

    result = await executor.execute(task, target)

    assert result.usage is not None
    assert result.cost_usd is None and result.cost_source is None


# ------------------------------------------------------- unpriced-model warning


def _trial(
    task_id: str,
    target_id: str,
    model: str | None,
    *,
    usage: UsageRecord | None = None,
    cost_usd: float | None = None,
) -> TrialResult:
    return TrialResult(
        trial_id=f"trial_{target_id}_{task_id}",
        run_id=RUN_ID,
        benchmark_id="rb_b_w3",
        task_id=task_id,
        target_id=target_id,
        model=model,
        outcome=TrialOutcome.UNSOLVED,
        usage=usage,
        cost_usd=cost_usd,
        cost_source="CATALOG_ESTIMATE" if cost_usd is not None else None,
    )


def _storage_with(tmp_path: Path, trials: list[TrialResult]) -> Storage:
    storage = Storage(tmp_path / "state.db")
    storage.create_run(RUN_ID, "rb_b_w3")
    for trial in trials:
        storage.save_trial(trial)
    return storage


def test_report_warns_when_target_has_no_cost_and_no_pricing(tmp_path: Path) -> None:
    storage = _storage_with(tmp_path, [_trial("task_1", "glm", "zai/glm-x")])

    data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)

    assert any(
        "target glm" in warning
        and "zai/glm-x" in warning
        and "reported no cost and has no usable pricing" in warning
        for warning in data.warnings
    )


def test_report_warns_when_tokens_reported_but_model_unpriced(tmp_path: Path) -> None:
    storage = _storage_with(
        tmp_path,
        [_trial("task_1", "odd", "openrouter/minimax-x", usage=UsageRecord(input_tokens=10))],
    )

    data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)

    assert any("target odd" in w and "reported no cost" in w for w in data.warnings)


def test_report_no_warning_when_user_pricing_covers_model(tmp_path: Path) -> None:
    cfg = RepoBenchConfig()
    cfg.pricing = {"zai/glm-x": PricingRule(input_per_million=1.0, output_per_million=1.0)}
    storage = _storage_with(tmp_path, [_trial("task_1", "glm", "zai/glm-x")])

    data = build_report_data(tmp_path, cfg, storage, run_id=RUN_ID)

    assert not any("target glm" in w for w in data.warnings)


def test_report_no_warning_when_catalog_covers_model_with_usage(tmp_path: Path) -> None:
    # The catalog can only price tokens that were actually reported.
    storage = _storage_with(
        tmp_path,
        [_trial("task_1", "glm", "glm-4.6", usage=UsageRecord(input_tokens=10))],
    )

    data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)

    assert not any("target glm" in w for w in data.warnings)


def test_report_warns_when_catalog_model_reports_no_usage(tmp_path: Path) -> None:
    # Catalog-known model + zero usage = still silently costless → warn.
    storage = _storage_with(tmp_path, [_trial("task_1", "glm", "glm-4.6")])

    data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)

    assert any(
        "target glm" in w and "reported no cost" in w for w in data.warnings
    )


def test_report_no_warning_when_cost_was_reported(tmp_path: Path) -> None:
    storage = _storage_with(
        tmp_path,
        [
            _trial(
                "task_1",
                "glm",
                "zai/glm-x",
                usage=UsageRecord(input_tokens=10, output_tokens=5),
                cost_usd=0.5,
            )
        ],
    )

    data = build_report_data(tmp_path, RepoBenchConfig(), storage, run_id=RUN_ID)

    assert not any("target glm" in w for w in data.warnings)


# ------------------------------------------------- catalog estimates in reports


def test_aggregation_keeps_and_mixes_catalog_estimate_source() -> None:
    """CATALOG_ESTIMATE flows through TargetMetrics aggregation like any other
    source, and mixes honestly when sources differ (issue #17)."""

    def _trial_for(target_id: str, task_id: str, source: str | None) -> TrialResult:
        return TrialResult(
            trial_id=f"trial_{target_id}_{task_id}",
            run_id=RUN_ID,
            task_id=task_id,
            target_id=target_id,
            model="glm-4.6",
            outcome=TrialOutcome.SOLVED,
            usage=UsageRecord(input_tokens=10, output_tokens=5),
            cost_usd=0.1,
            cost_source=source,
        )

    catalog_only = aggregate_trials(
        [
            _trial_for("glm", "task_1", "CATALOG_ESTIMATE"),
            _trial_for("glm", "task_2", "CATALOG_ESTIMATE"),
        ]
    )["glm"]
    assert catalog_only.total_cost_usd == pytest.approx(0.2)
    assert catalog_only.cost_source == "CATALOG_ESTIMATE"

    mixed = aggregate_trials(
        [
            _trial_for("glm", "task_1", "CATALOG_ESTIMATE"),
            _trial_for("glm", "task_2", "USER_PROVIDED_PRICING"),
        ]
    )["glm"]
    assert mixed.cost_source == "MIXED"


def _metrics(target_id: str, cost_source: str | None) -> TargetMetrics:
    wilson_lo, wilson_hi = wilson_ci(5, 10)
    return TargetMetrics(
        target_id=target_id,
        n=10,
        solved=5,
        solve_rate=0.5,
        time_p50_ms=1000,
        time_p90_ms=2000,
        timeouts=0,
        errors=0,
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cost_usd=1.0,
        cost_source=cost_source,
        cost_per_solve_usd=0.1,
        effective_cost_usd=0.1,
        mean_files_changed=None,
        wilson_lo=wilson_lo,
        wilson_hi=wilson_hi,
    )


def _report_data(target_id: str, cost_source: str | None) -> ReportData:
    return ReportData(
        benchmark_id=None,
        repository=None,
        run_id=RUN_ID,
        tasks_total=0,
        health=None,
        targets=[_metrics(target_id, cost_source)],
        comparisons=[],
        recommendation=None,
        segments={},
        warnings=[],
        concurrency=1,
    )


def test_terminal_report_marks_catalog_estimate_costs() -> None:
    text = render_report(_report_data("glm", "CATALOG_ESTIMATE"))
    assert "~$0.10" in text
    assert "bundled pricing catalog" in text

    # harness-reported / user-priced costs keep the hard-number form
    plain = render_report(_report_data("glm", "HARNESS_REPORTED"))
    assert "~$0.10" not in plain
    assert "$0.10" in plain
    assert "bundled pricing catalog" not in plain


# ---------------------------------------------------- targets list PRICING column


def test_target_pricing_label_states() -> None:
    cfg = RepoBenchConfig()
    cfg.pricing = {"glm-4.6": PricingRule(input_per_million=1.0, output_per_million=2.0)}
    user_target = ExecutionTarget(harness="opencode", model="glm-4.6")
    catalog_target = ExecutionTarget(harness="claude", model="claude-sonnet-4-5-20250929")
    unpriced_target = ExecutionTarget(harness="command", command=["agent"])

    assert _target_pricing_label(user_target, cfg) == "user"
    assert _target_pricing_label(catalog_target, cfg) == "catalog~"
    assert _target_pricing_label(unpriced_target, cfg) == "—"


def test_targets_list_shows_pricing_column(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_repo)
    cfg = RepoBenchConfig()
    cfg.project = ProjectConfig(language="python", test_command="python -m pytest")
    cfg.targets.update(
        {
            "priced": ExecutionTarget(harness="claude", model="claude-sonnet-4-5"),
            "estimated": ExecutionTarget(harness="codex", model="gpt-5.1"),
            "unknown": ExecutionTarget(harness="gemini", model="mystery-model"),
        }
    )
    cfg.pricing = {
        "claude-sonnet-4-5": PricingRule(input_per_million=3.0, output_per_million=15.0)
    }
    cfg.save(fixture_repo / "repobench.yml")

    result = CliRunner().invoke(app, ["targets", "list"])

    assert result.exit_code == 0, result.output
    assert "PRICING" in result.output
    assert re.search(r"\buser\b", result.output)
    assert "catalog~" in result.output
    assert "—" in result.output
