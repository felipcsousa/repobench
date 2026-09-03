"""Rich rendering for the RepoBench CLI (PRD §10, §88, §91-93, §96, §111).

All dynamic content is printed with markup disabled — repository paths and
task ids may contain characters Rich would otherwise interpret.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table as RichTable

from repobench.config import RepoBenchConfig, detect_subprojects
from repobench.core.types import AnalyzeSummary, CandidateInfo, ExecutionTarget
from repobench.execution.pricing_catalog import lookup as catalog_lookup

# Never truncate data on a pipe: terminals get their real width, captured output
# (tests, CI) gets plenty of room so table cells are never ellipsized.
console = Console(width=None if sys.stdout.isatty() else 200)

OK = "✓"
MISS = "✗"
WARN = "⚠"


def echo(message: str = "", flush: bool = False) -> None:
    console.print(message, markup=False, highlight=False, soft_wrap=True)
    # Piped stdout is block-buffered, so long-running steps must flush after
    # each line or the user stares at silence until the process exits.
    if flush:
        sys.stdout.flush()


def fail(message: str) -> None:
    echo(f"error: {message}")


def kv(label: str, value: str) -> None:
    # The explicit space keeps long labels (>= 28 cols) from abutting the value;
    # for shorter labels the padding absorbs it and the column is unchanged.
    echo(f"{label:<27} {value}")


def _target_provider(target: ExecutionTarget) -> str:
    """Explicit provider field, or the model's "provider/model" prefix, else inherited."""
    if target.provider:
        return target.provider
    if target.model and "/" in target.model:
        return target.model.split("/", 1)[0]
    return "inherited"


def _merge_style_label(styles) -> str:  # noqa: ANN001 - MergeStyleCounts
    """e.g. "12 merge commits · 13 squash"; zero-count styles are omitted so the
    line never claims work the repository does not have."""
    parts = []
    if styles.merge_commits:
        parts.append(f"{styles.merge_commits} merge commits")
    if styles.squash:
        parts.append(f"{styles.squash} squash")
    return " · ".join(parts)


def _recall_label(mined: int, total: int) -> str:
    counts = f"{mined}/{total} merged PRs"
    return f"{counts} ({round(100 * mined / total)}%)" if total > 0 else counts


def render_analyze_summary(
    outcome, suggested_size: int, lookback_note: str
) -> None:  # noqa: ANN001 - AnalyzeOutcome (avoids import cycle)
    summary: AnalyzeSummary = outcome.summary
    echo("RepoBench analyzed your repository")
    echo("")
    kv("Merged PRs", f"{outcome.merged_prs}")
    # Issue #31 fields — getattr keeps the renderer usable with older outcomes.
    styles = getattr(outcome, "merge_styles", None)
    if styles is not None and styles.merge_commits + styles.squash > 0:
        kv("Merge style", _merge_style_label(styles))
    recall_total = getattr(outcome, "recall_total", None)
    if recall_total is not None:
        kv("Recall vs GitHub", _recall_label(outcome.merged_prs, recall_total))
    kv("Potential task candidates", f"{summary.task_candidates}")
    # Issue #35 honesty fix: analyze never validates anything (that happens in
    # `benchmark build`), so the label must not claim it. The field name
    # (`validated_candidates`) predates validation and only counts candidates
    # that survived the mining hard filters.
    kv("Candidates passing hard filters", str(summary.validated_candidates))
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


def render_merge_style_warnings(outcome) -> None:  # noqa: ANN001 - AnalyzeOutcome
    """Issue #31 field-credibility notes: squash-merged PRs are mined from
    commit subjects, and — only when gh ground truth exists — recall low enough
    to prove some history is invisible (rebase merges carry no PR number in git
    at all). Without gh, nothing is claimed about recall."""
    styles = getattr(outcome, "merge_styles", None)
    if styles is not None and styles.squash:
        plural = "s" if styles.squash != 1 else ""
        echo(
            f"{WARN} merge style: {styles.squash} squash-merged PR{plural} mined "
            "from commit subjects (#N) — no merge commits for them exist"
        )
    recall_total = getattr(outcome, "recall_total", None)
    if not recall_total:  # None (no gh) or 0 merged PRs — no honest recall claim
        return
    mined = outcome.merged_prs
    if recall_total > 0 and round(100 * mined / recall_total) < 70:
        missing = recall_total - mined
        plural = "s" if missing != 1 else ""
        echo(
            f"{WARN} low recall: {missing} of {recall_total} merged PR"
            f"{plural} in the window are invisible to mining — PRs merged by "
            "rebase carry no PR number in git at all"
        )


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


def _decode_details(raw: str | None) -> str | None:
    """The task_validations column is named details_json but predates it:
    rows store the plain check text (output tails included), never JSON.
    Decode defensively so either encoding renders honestly (issue #35)."""
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(decoded, str):
        return decoded
    return json.dumps(decoded, ensure_ascii=False)


def render_pr_diagnostics(
    pr_number: int,
    candidates: list[CandidateInfo],
    task_rows: list[dict],
    history_by_task: dict[str, list[dict]],
) -> None:
    """`candidates --show <PR>` (issue #35): why a PR became what it is.
    Filtered before packaging → the mining rejection code; otherwise each
    task's task_validations log (check, outcome, details with output tails) —
    the diagnostics that used to require opening state.db by hand."""

    def status_line(candidate: CandidateInfo) -> str:
        line = candidate.status.value
        if candidate.rejection_code is not None:
            line += f" · rejection {candidate.rejection_code.value}"
        return line

    header = f"Candidate PR #{pr_number}"
    if len(candidates) == 1:
        header += f" — {status_line(candidates[0])}"
    echo(header)
    echo("")
    if not candidates:
        echo("(no candidate row recorded for this PR)")
    elif len(candidates) > 1:
        for candidate in candidates:
            echo(f"  {status_line(candidate)}")
        echo("")
    if not task_rows:
        echo("No tasks were packaged for this PR — it was filtered during mining,")
        echo("before validation ran.")
        return
    for index, row in enumerate(task_rows):
        if index:
            echo("")
        echo(f"Task {row['task_id']} (version {row['version']}) — {row['status']}")
        checks = history_by_task.get(row["task_id"], [])
        if not checks:
            echo("  (no validation checks recorded)")
            continue
        for check in checks:
            mark = {"passed": OK, "failed": MISS}.get(check["result"], "·")
            echo(f"  {mark} {check['kind']:<16}{check['result']}")
            details = _decode_details(check["details_json"])
            if details:
                for detail_line in details.splitlines() or [""]:
                    echo(f"      {detail_line}")


def _target_pricing_label(target: ExecutionTarget, cfg: RepoBenchConfig) -> str:
    """Issue #17: where the target's cost will come from — the user's
    `pricing:` rule, the bundled catalog (`~` marks an estimate), or nothing."""
    if target.model and target.model in cfg.pricing:
        return "user"
    if catalog_lookup(target.model) is not None:
        return "catalog~"
    return "—"


def render_targets_table(cfg: RepoBenchConfig) -> None:
    table = RichTable(header_style="bold")
    for column in ("TARGET", "HARNESS", "MODEL", "PROVIDER", "PRICING"):
        table.add_column(column)
    for target in cfg.targets.values():
        table.add_row(
            target.id,
            target.harness,
            target.model or "(harness default)",
            _target_provider(target),
            _target_pricing_label(target, cfg),
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
    if getattr(outcome, "reuse_valid", False):
        # Incremental builds only (issue #16): how many tasks skipped revalidation.
        kv("Reused valid tasks", str(outcome.reused))
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
    # Issue #19: None means the persisted health predates the component — no
    # invented score (renderers must handle it).
    verifier = outcome.health.verifier_strength
    kv("Verifier strength", "—" if verifier is None else str(verifier))
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


def render_benchmark_refresh(outcome) -> None:  # noqa: ANN001 - RefreshOutcome
    """`benchmark refresh` (issue #15, PRD §148): the drift story first, then
    the task turnover of the rebuild. The Reason block only appears when a
    derived reason exists — nothing is invented."""
    drift = outcome.drift
    delta = drift.overall_after - drift.overall_before
    coverage_line = (
        f"Coverage: {drift.overall_before} → {drift.overall_after} ({delta:+d})"
    )
    echo("Benchmark refresh")
    echo("")
    echo(f"{outcome.old_benchmark_id} → {outcome.new_benchmark_id}")
    echo("")
    echo("Drift")
    echo("")
    if drift.drifted:
        echo(coverage_line)
    else:
        echo(f"{coverage_line} — benchmark still representative")
    dims = drift.per_dimension
    echo(
        f"task_type {dims['task_type'][0]} → {dims['task_type'][1]}"
        f" · subsystem {dims['subsystem'][0]} → {dims['subsystem'][1]}"
        f" · complexity {dims['complexity'][0]} → {dims['complexity'][1]}"
    )
    if drift.reasons:
        echo("Reason:")
        for reason in drift.reasons:
            echo(reason)
    echo("")
    tasks_line = (
        f"Tasks: {outcome.new_tasks} new · {outcome.retained_tasks} retained"
        f" · {len(outcome.build.sample)} total"
    )
    if outcome.missing_tasks:
        tasks_line += f" · {outcome.missing_tasks} missing"
    echo(tasks_line)
    if getattr(outcome.build, "reuse_valid", False):
        # Incremental refresh only (issue #16): how many tasks skipped revalidation.
        kv("Reused valid tasks", str(outcome.build.reused))
    echo(f"manifest: {outcome.new_manifest_path}")


def render_public_repository_warning() -> None:
    """The PRD §51 contamination warning block, verbatim in spirit."""
    echo("")
    echo("⚠ PUBLIC REPOSITORY")
    echo("")
    echo("This benchmark cannot guarantee that the tested model or agent has")
    echo("never seen the repository, its issues or the solution.")
    echo("")
    echo("Results measure practical performance, not contamination-free capability.")
    echo("")


def render_run_preview(plan, benchmark_note: str) -> None:  # noqa: ANN001 - RunPlan
    import shlex

    echo("")
    kv("Benchmark", plan.benchmark_id)
    kv("Tasks", str(len(plan.tasks)))
    kv("Targets", ", ".join(t.id for t in plan.targets))
    kv("Trials", str(len(plan.pairs)))
    if plan.rollouts > 1:
        # PRD §103: the cost multiplier of multiple rollouts must be explicit.
        kv("Rollouts", f"{plan.rollouts} per Task×Target (cost ×{plan.rollouts})")
    if plan.already_complete:
        kv("Already complete", f"{plan.already_complete} (resuming)")
    if getattr(plan, "retried", 0):
        kv("Retrying", f"{plan.retried} previous trial(s)")
    # Generic-command templates are always shown before execution (PRD §26).
    for target in plan.targets:
        if target.harness == "command":
            kv(f"command[{target.id}]", shlex.join(target.command or []))
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
    # flushed: benchmark builds take minutes per candidate, and piped output
    # must still show each line the moment it is produced.
    echo(f"  {message}", flush=True)


# --------------------------------------------------------------- runs / clean


def _short_when(value: str | None) -> str:
    return value.replace("T", " ")[:16] if value else "—"


def render_runs_table(views) -> None:  # noqa: ANN001 - list[RunRowView]
    table = RichTable(header_style="bold")
    for column in ("RUN", "BENCHMARK", "STATUS", "STARTED", "FINISHED", "TARGETS", "TRIALS"):
        table.add_column(column)
    for view in views:
        table.add_row(
            view.run_id,
            view.benchmark_id or "—",
            view.status,
            _short_when(view.started_at),
            _short_when(view.finished_at),
            str(view.targets),
            f"{view.trials_done} ({view.trials_solved} solved)",
        )
    console.print(table)


def render_run_show(view) -> None:  # noqa: ANN001 - RunShowView
    echo(f"Run {view.row.run_id}")
    echo("")
    kv("Benchmark", view.row.benchmark_id or "—")
    kv("Status", view.row.status)
    kv("Started", _short_when(view.row.started_at))
    kv("Finished", _short_when(view.row.finished_at))
    echo("")
    if not view.targets:
        echo("(no trials recorded for this run)")
        return
    table = RichTable(header_style="bold")
    for column in ("TARGET", "N", "SOLVED", "SOLVE RATE", "TESTS", "P50", "TIMEOUTS", "ERRORS"):
        table.add_column(column)

    def duration(ms) -> str:  # noqa: ANN001
        return f"{ms / 1000:.0f}s" if ms is not None else "—"

    def tests_cell(metrics) -> str:  # noqa: ANN001 - TargetMetrics
        """Mean passed over mean (total - skipped); — when no trial carried
        per-test counts — a number is never invented (PRD honesty rule)."""
        if (
            metrics.tests_partial_n == 0
            or metrics.tests_mean_passed is None
            or metrics.tests_mean_denominator is None
        ):
            return "—"
        return f"{metrics.tests_mean_passed:.0f}/{metrics.tests_mean_denominator:.0f}"

    for metrics in view.targets:
        table.add_row(
            metrics.target_id,
            str(metrics.n),
            str(metrics.solved),
            f"{metrics.solve_rate * 100:.0f}%",
            tests_cell(metrics),
            duration(metrics.time_p50_ms),
            str(metrics.timeouts),
            str(metrics.errors),
        )
    console.print(table)


def render_clean_plan(plan, *, apply: bool) -> None:  # noqa: ANN001 - CleanPlan
    if apply:
        echo("repobench clean — removing")
    else:
        echo("repobench clean — would remove (dry-run; pass --apply to execute)")
    echo("")
    if plan.empty:
        echo("nothing to clean")
        return
    for run_id in plan.run_ids:
        echo(f"  run      {run_id} (artifacts + trials + run row)")
    for directory in plan.workspace_dirs:
        echo(f"  workspace {directory.name}/")
    if plan.cache_dir is not None:
        echo(f"  cache     {plan.cache_dir.name}/")
    echo("")
    echo(f"approx. {plan.freed_bytes / 1_048_576:.1f} MB freed")


def subproject_summary_lines(root: Path) -> list[str]:
    """Issue #34: one honest line per detected sub-project — the backend the
    old root-only detection silently ignored. Empty when none exist, so callers
    never print a monorepo header for a single-project repo. A sub-project
    without a test command says so instead of inventing one."""
    projects = detect_subprojects(root)
    if not projects:
        return []
    lines = [
        "sub-projects detected (one command set benchmarks — project.cwd picks where it runs):"
    ]
    for project in projects:
        cfg = project.config
        identity = [project.path, cfg.language or "unknown"]
        if cfg.package_manager:
            identity.append(cfg.package_manager)
        if cfg.install_command:
            identity.append(f"install: {cfg.install_command}")
        if cfg.test_command:
            identity.append(f"test: {cfg.test_command}")
        else:
            identity.append("no test command — set project.test_command or project.cwd")
        lines.append("  " + " · ".join(identity))
    return lines


def config_summary_lines(cfg: RepoBenchConfig, root: Path) -> list[str]:
    project = cfg.project
    lines = [
        f"repo:      {root}",
        f"language:  {project.language or 'not detected'}",
        f"packages:  {project.package_manager or 'not detected'}",
    ]
    if project.cwd:
        # Issue #34: the monorepo knob is part of the summary so a configured
        # sub-directory is always visible next to the commands it redirects.
        lines.append(f"cwd:       {project.cwd}")
    if project.install_command:
        lines.append(f"install:   {project.install_command}")
    if project.test_command:
        lines.append(f"test:      {project.test_command}")
    elif project.language:
        # Issue #34 honesty: say so instead of implying `npm test` exists —
        # an invented suggestion guarantees BASELINE_BROKEN at first build.
        lines.append("test:      none detected — set project.test_command in repobench.yml")
    # Issue #34: surface every sub-project so the ignored backend is seen
    # before the first build, not after a guaranteed-broken run.
    lines.extend(subproject_summary_lines(root))
    lines.append("edit repobench.yml to adjust commands, targets and benchmark size.")
    return lines


# ------------------------------------------------------------------- compare


def render_compare(outcome) -> None:  # noqa: ANN001 - CompareOutcome
    """`repobench compare` (PRD §149, issue #14): overall B − A deltas with
    paired-bootstrap CIs, cost drift, pooled segment drift and warnings."""
    echo("Compare runs")
    echo("")
    echo(f"{outcome.run_a} → {outcome.run_b} (benchmark {outcome.benchmark_id or '—'})")
    if outcome.tasks_only_a or outcome.tasks_only_b:
        echo(
            f"tasks only in A: {outcome.tasks_only_a} · "
            f"tasks only in B: {outcome.tasks_only_b}"
        )
    echo("")
    echo("Overall")
    echo("")
    if not outcome.targets:
        echo("(no trials recorded for either run)")
    for delta in outcome.targets:
        verdict = "conclusive" if delta.conclusive else "not conclusive"
        echo(
            f"{delta.target_id}  {delta.rate_a * 100:.0f}% → {delta.rate_b * 100:.0f}%"
            f"  ({delta.diff_pp:+.0f}pp)  "
            f"[95% CI {delta.ci_lo_pp:+.0f}pp → {delta.ci_hi_pp:+.0f}pp, {verdict}]"
        )
    echo("")
    echo("Cost")
    echo("")
    cost_rows = [
        delta
        for delta in outcome.targets
        if delta.cost_a is not None
        and delta.cost_b is not None
        and delta.cost_delta_pct is not None
    ]
    if cost_rows:
        for delta in cost_rows:
            echo(
                f"{delta.target_id}  ${delta.cost_a:.2f} → ${delta.cost_b:.2f}"
                f"  ({delta.cost_delta_pct:+.0f}%)"
            )
    else:
        # Costs are only shown when both runs reported them — never invented.
        echo("(no cost reported by both runs)")
    for dimension, deltas in outcome.segments.items():
        echo("")
        echo(f"Segments — {dimension}")
        echo("")
        if not deltas:
            echo("(no segments shared by both runs)")
        for delta in deltas:
            echo(
                f"{delta.segment:<16}{delta.rate_a * 100:.0f}% → {delta.rate_b * 100:.0f}%"
                f"  ({delta.diff_pp:+.0f}pp)"
            )
    if outcome.warnings:
        echo("")
        echo("Warnings")
        echo("")
        for warning in outcome.warnings:
            echo(f"{WARN} {warning}")
