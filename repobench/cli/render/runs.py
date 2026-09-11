"""Run execution and cleanup renderers (PRD §96, §103, §111; issues #4/#9)."""

from __future__ import annotations

from rich.table import Table as RichTable

from repobench.cli.render.base import console, echo, kv


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
