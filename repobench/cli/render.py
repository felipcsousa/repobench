"""Rich rendering for the RepoBench CLI (PRD §10, §88, §91-93, §96, §111).

All dynamic content is printed with markup disabled — repository paths and
task ids may contain characters Rich would otherwise interpret.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table as RichTable

from repobench.config import RepoBenchConfig
from repobench.core.types import AnalyzeSummary, CandidateInfo, ExecutionTarget

# Never truncate data on a pipe: terminals get their real width, captured output
# (tests, CI) gets plenty of room so table cells are never ellipsized.
console = Console(width=None if sys.stdout.isatty() else 200)

OK = "✓"
MISS = "✗"
WARN = "⚠"


def echo(message: str = "") -> None:
    console.print(message, markup=False, highlight=False, soft_wrap=True)


def fail(message: str) -> None:
    echo(f"error: {message}")


def kv(label: str, value: str) -> None:
    echo(f"{label:<28}{value}")


def _target_provider(target: ExecutionTarget) -> str:
    """Explicit provider field, or the model's "provider/model" prefix, else inherited."""
    if target.provider:
        return target.provider
    if target.model and "/" in target.model:
        return target.model.split("/", 1)[0]
    return "inherited"


def render_analyze_summary(
    outcome, suggested_size: int, lookback_note: str
) -> None:  # noqa: ANN001 - AnalyzeOutcome (avoids import cycle)
    summary: AnalyzeSummary = outcome.summary
    echo("RepoBench analyzed your repository")
    echo("")
    kv("Merged PRs", f"{outcome.merged_prs}")
    kv("Potential task candidates", f"{summary.task_candidates}")
    kv("Eval candidates", str(sum(1 for c in outcome.candidates if c.status.value == "DISCOVERED")))
    echo("")
    echo("Workload")
    echo("")
    workload = summary.workload
    shares: dict[str, float] = {}
    for key, share in workload.task_type.items():
        shares[key] = shares.get(key, 0.0) + share
    if shares:
        for task_type, share in sorted(shares.items(), key=lambda item: -item[1]):
            echo(f"{task_type:<28}{round(share * 100):>3.0f}%")
    else:
        echo("(no merged PRs in the lookback window)")
    echo("")
    echo("Suggested benchmark")
    echo("")
    kv("Tasks", f"{suggested_size}")
    echo("")
    echo(f"{lookback_note}")
    echo("No inference tokens were consumed.")


def render_candidates_table(candidates: list[CandidateInfo]) -> None:
    table = RichTable(header_style="bold", show_lines=False)
    for column in (
        "PR",
        "Type",
        "Subsystem",
        "Complexity",
        "Impl LOC",
        "Test LOC",
        "Instr.",
        "Status",
        "Rejection",
    ):
        table.add_column(column)
    for candidate in candidates:
        assessment = candidate.assessment
        table.add_row(
            str(candidate.pr.number),
            assessment.task_type.value,
            assessment.subsystem,
            assessment.complexity.value,
            str(assessment.implementation_loc),
            str(assessment.test_loc),
            assessment.instruction_confidence,
            candidate.status.value,
            candidate.rejection_code.value if candidate.rejection_code else "—",
        )
    console.print(table)


def render_targets_table(cfg: RepoBenchConfig) -> None:
    table = RichTable(header_style="bold")
    for column in ("TARGET", "HARNESS", "MODEL", "PROVIDER"):
        table.add_column(column)
    for target in cfg.targets.values():
        table.add_row(
            target.id,
            target.harness,
            target.model or "(harness default)",
            _target_provider(target),
        )
    console.print(table)


def provider_label(target: ExecutionTarget) -> str:
    return _target_provider(target)


def render_capability_row(name: str, caps) -> str:  # noqa: ANN001 - HarnessCapabilities
    def mark(flag: bool) -> str:
        return OK if flag else "✗"

    name_column = 12
    return (
        f"{name:<{name_column}}"
        f"{mark(caps.model_override):^7}"
        f"{mark(caps.structured_output):^7}"
        f"{mark(caps.token_usage):^7}"
        f"{mark(caps.cost_usage):^7}"
        f"{mark(caps.custom_provider):^10}"
    )


def render_benchmark_build(outcome) -> None:  # noqa: ANN001 - BenchmarkBuildOutcome
    echo("")
    kv("Valid candidates", str(len(outcome.valid)))
    kv("Requested benchmark size", str(outcome.requested_size))
    echo("")
    if len(outcome.valid) < outcome.requested_size:
        echo(
            f"note: only {len(outcome.valid)} valid task(s) available "
            f"(requested {outcome.requested_size}); the benchmark uses all of them."
        )
        echo("")
    echo("Benchmark")
    echo(outcome.benchmark_id)
    echo("")
    kv("Tasks", str(len(outcome.sample)))
    if getattr(outcome, "instruction_tiers", None):
        tiers = " ".join(
            f"{tier}×{count}"
            for tier, count in sorted(outcome.instruction_tiers.items())
        )
        kv("Instruction tiers", tiers)
    echo("")
    kv("Health", str(outcome.health.overall))
    echo("")
    kv("Representativeness", str(outcome.health.representativeness))
    kv("Validation", str(outcome.health.validation_confidence))
    kv("Leakage", str(outcome.health.leakage_resistance))
    kv("Recency", str(outcome.health.recency))
    kv("Diversity", str(outcome.health.diversity))
    echo("")
    echo("Warnings")
    echo("")
    for warning in outcome.health.warnings:
        echo(f"{WARN} {warning}")
    echo("")
    echo(f"manifest: {outcome.manifest_path}")


def render_run_preview(plan, benchmark_note: str) -> None:  # noqa: ANN001 - RunPlan
    echo("")
    kv("Benchmark", plan.benchmark_id)
    kv("Tasks", str(len(plan.tasks)))
    kv("Targets", ", ".join(t.id for t in plan.targets))
    kv("Trials", str(len(plan.pairs)))
    if plan.already_complete:
        kv("Already complete", f"{plan.already_complete} (resuming)")
    kv("Execution", "local")
    kv("Concurrency", str(plan.jobs))
    kv("Timeout", f"{plan.timeout_minutes}m / trial")
    kv("Network isolation", "none")
    echo("")
    echo(benchmark_note)


def render_progress(done: int, total: int, trial) -> None:  # noqa: ANN001 - TrialResult
    duration = trial.duration_ms / 1000
    echo(
        f"  [{done}/{total}] {trial.task_id} · {trial.target_id} · "
        f"{trial.outcome.value} · {duration:.1f}s"
    )


def render_run_summary(results: list) -> None:  # noqa: ANN001 - list[TrialResult]
    from collections import Counter

    echo("")
    echo("Results")
    echo("")
    by_target: dict[str, Counter] = {}
    for trial in results:
        by_target.setdefault(trial.target_id, Counter())[trial.outcome.value] += 1
    for target_id in sorted(by_target):
        counts = by_target[target_id]
        solved = counts.get("SOLVED", 0)
        total = sum(counts.values())
        extras = ", ".join(
            f"{count} {outcome}" for outcome, count in sorted(counts.items()) if outcome != "SOLVED"
        )
        suffix = f" ({extras})" if extras else ""
        echo(f"  {target_id:<24}{solved}/{total} solved{suffix}")
    if not results:
        echo("  (no trials executed)")


def render_task_build_line(message: str) -> None:
    echo(f"  {message}")


def config_summary_lines(cfg: RepoBenchConfig, root: Path) -> list[str]:
    project = cfg.project
    lines = [
        f"repo:      {root}",
        f"language:  {project.language or 'not detected'}",
        f"packages:  {project.package_manager or 'not detected'}",
    ]
    if project.install_command:
        lines.append(f"install:   {project.install_command}")
    if project.build_command:
        lines.append(f"build:     {project.build_command}")
    if project.test_command:
        lines.append(f"test:      {project.test_command}")
    lines.append("edit repobench.yml to adjust commands, targets and benchmark size.")
    return lines
