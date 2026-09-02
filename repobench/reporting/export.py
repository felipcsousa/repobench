"""Trial-level export: JSONL and CSV (PRD §112).

`report --format jsonl|csv` emits one row per TrialResult — optionally joined
with task metadata (PR number, task type, subsystem, complexity, instruction
tier) — so teams can load runs straight into pandas/BigQuery. JSONL rows are
full TrialResult documents; CSV flattens them into stable columns.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from repobench.core.types import TaskMetadata, TrialResult

# Flat CSV column contract; keys of _export_row() must match exactly (guarded
# by test_export_row_covers_csv_columns).
CSV_COLUMNS: tuple[str, ...] = (
    "trial_id",
    "run_id",
    "benchmark_id",
    "task_id",
    "target_id",
    "rollout",
    "harness",
    "harness_version",
    "model",
    "provider",
    "outcome",
    "started_at",
    "duration_ms",
    "exit_code",
    "timed_out",
    "task_verified",
    "regression_verified",
    "usage.input_tokens",
    "usage.cached_input_tokens",
    "usage.output_tokens",
    "usage.reasoning_tokens",
    "usage.requests",
    "usage.tool_calls",
    "usage.reported_cost_usd",
    "cost_usd",
    "cost_source",
    "files_changed",
    "loc_added",
    "loc_removed",
    "tampered_tests",
    "task.pr_number",
    "task.task_type",
    "task.subsystem",
    "task.complexity",
    "task.instruction_confidence",
)


def _task_fields(task: TaskMetadata) -> dict[str, Any]:
    """The task-metadata join shared by both export formats (issue #5)."""
    return {
        "pr_number": task.pr_number,
        "task_type": task.assessment.task_type.value,
        "subsystem": task.assessment.subsystem,
        "complexity": task.assessment.complexity.value,
        "instruction_confidence": task.assessment.instruction_confidence,
    }


def _export_row(trial: TrialResult, task: TaskMetadata | None) -> dict[str, Any]:
    """One trial as {CSV column: value} — explicit, no generic path walking."""
    usage = trial.usage
    row: dict[str, Any] = {
        "trial_id": trial.trial_id,
        "run_id": trial.run_id,
        "benchmark_id": trial.benchmark_id,
        "task_id": trial.task_id,
        "target_id": trial.target_id,
        "rollout": trial.rollout,
        "harness": trial.harness,
        "harness_version": trial.harness_version,
        "model": trial.model,
        "provider": trial.provider,
        "outcome": trial.outcome.value,
        "started_at": trial.started_at.isoformat() if trial.started_at else None,
        "duration_ms": trial.duration_ms,
        "exit_code": trial.exit_code,
        "timed_out": trial.timed_out,
        "task_verified": trial.task_verified,
        "regression_verified": trial.regression_verified,
        "usage.input_tokens": usage.input_tokens if usage else None,
        "usage.cached_input_tokens": usage.cached_input_tokens if usage else None,
        "usage.output_tokens": usage.output_tokens if usage else None,
        "usage.reasoning_tokens": usage.reasoning_tokens if usage else None,
        "usage.requests": usage.requests if usage else None,
        "usage.tool_calls": usage.tool_calls if usage else None,
        "usage.reported_cost_usd": usage.reported_cost_usd if usage else None,
        "cost_usd": trial.cost_usd,
        "cost_source": trial.cost_source,
        "files_changed": trial.files_changed,
        "loc_added": trial.loc_added,
        "loc_removed": trial.loc_removed,
        # issue #18: test files the agent's diff touched, `;`-joined (a finding,
        # independent of the outcome columns)
        "tampered_tests": ";".join(trial.tampered_tests),
    }
    if task is not None:
        row.update({f"task.{key}": value for key, value in _task_fields(task).items()})
    return row


def _csv_value(value: Any) -> Any:
    """None → empty cell; bools in lowercase json style; everything else as-is."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def render_jsonl(
    trials: list[TrialResult], tasks: dict[str, TaskMetadata] | None = None
) -> str:
    """One TrialResult JSON document per line (model_dump round-trips exactly)."""
    tasks = tasks or {}
    lines: list[str] = []
    for trial in trials:
        document = json.loads(trial.model_dump_json())
        task = tasks.get(trial.task_id)
        if task is not None:
            document["task"] = _task_fields(task)
        lines.append(json.dumps(document, sort_keys=False))
    return "\n".join(lines) + ("\n" if lines else "")


def render_csv(
    trials: list[TrialResult], tasks: dict[str, TaskMetadata] | None = None
) -> str:
    """Flat CSV with one row per trial and a stable column contract."""
    tasks = tasks or {}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for trial in trials:
        row = _export_row(trial, tasks.get(trial.task_id))
        writer.writerow([_csv_value(row.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()
