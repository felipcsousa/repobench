"""Flakiness estimation from the append-only validation history (issue #19).

`task_validations` is never rewritten, so a task whose SAME check kind produced
both a "passed" and a "failed" row must have flip-flopped between build
sessions — that is the flakiness signal. Rows written inside one build cannot
flip by construction: the validator stops at the first failing check and each
check kind is appended at most once per build (the reuse path appends its own
kind once, too), so no build-session grouping is needed.
"""

from __future__ import annotations

import pydantic

DECISIVE_RESULTS = ("passed", "failed")


class FlakinessReport(pydantic.BaseModel):
    """Outcome of scanning validation history for flip-flopping tasks (issue #19)."""

    # Task ids with at least one passed AND one failed row for the same kind.
    flaky_tasks: list[str]
    # flaky / tasks with at least one decisive (passed|failed) row — skipped
    # rows carry no verdict, so tasks only ever skipped cannot be judged.
    flaky_ratio: float
    tasks_seen: int


def flakiness_from_history(history_rows: list[dict]) -> FlakinessReport:
    """Pure scan of Storage.validation_history() rows (issue #19).

    Different check kinds never cross-contaminate: the flip is detected per
    (task_id, kind) pair. Missing history is no signal, not evidence of
    quality — an empty log yields ratio 0.0 with tasks_seen 0, never an
    invented number (PRD §53-54 honesty pattern).
    """
    outcomes: dict[tuple[str, str], set[str]] = {}
    for row in history_rows:
        task_id = row.get("task_id")
        kind = row.get("kind")
        result = row.get("result")
        if task_id is None or kind is None or result not in DECISIVE_RESULTS:
            continue
        outcomes.setdefault((task_id, kind), set()).add(result)

    flaky = sorted(
        {
            task_id
            for (task_id, _kind), seen in outcomes.items()
            if seen == set(DECISIVE_RESULTS)
        }
    )
    tasks_seen = {task_id for (task_id, _kind) in outcomes}
    ratio = (len(flaky) / len(tasks_seen)) if tasks_seen else 0.0
    return FlakinessReport(
        flaky_tasks=flaky,
        flaky_ratio=ratio,
        tasks_seen=len(tasks_seen),
    )
