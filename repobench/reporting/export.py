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

# Flat CSV column contract: dotted paths inside one TrialResult document,
# plus the task-metadata join (prefixed task.*).
CSV_COLUMNS: tuple[str, ...] = (
    "trial_id",
    "run_id",
    "benchmark_id",
    "task_id",
    "target_id",
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
    "task.pr_number",
    "task.task_type",
    "task.subsystem",
    "task.complexity",
    "task.instruction_confidence",
)


def _dig(document: dict, dotted_path: str) -> Any:
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _join_task(trial: TrialResult, tasks: dict[str, TaskMetadata]) -> dict:
    task = tasks.get(trial.task_id)
    if task is None:
        return {}
    assessment = task.assessment
    return {
        "task.pr_number": task.pr_number,
        "task.task_type": assessment.task_type.value,
        "task.subsystem": assessment.subsystem,
        "task.complexity": assessment.complexity.value,
        "task.instruction_confidence": assessment.instruction_confidence,
    }


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
            document["task"] = {
                "pr_number": task.pr_number,
                "task_type": task.assessment.task_type.value,
                "subsystem": task.assessment.subsystem,
                "complexity": task.assessment.complexity.value,
                "instruction_confidence": task.assessment.instruction_confidence,
            }
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
        document = json.loads(trial.model_dump_json())
        row = {**_join_task(trial, tasks)}
        for column in CSV_COLUMNS:
            if column not in row:
                value = _dig(document, column)
                row[column] = "" if value is None else value
        writer.writerow([row[column] for column in CSV_COLUMNS])
    return buffer.getvalue()
