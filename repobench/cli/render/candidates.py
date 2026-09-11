"""Candidate renderers (PRD §35-36, §71; the `candidates --show` validation
diagnostics from issue #35)."""

from __future__ import annotations

import json

from rich.table import Table as RichTable

from repobench.cli.render.base import MISS, OK, console, echo, kv
from repobench.core.types import CandidateClass, CandidateInfo, Complexity


def render_candidates_table(candidates: list[CandidateInfo]) -> None:
    table = RichTable(header_style="bold", show_lines=False)
    # Score/Class (PRD §35) render '—' for legacy hard_caps candidates that
    # were never scored — an unknown is never dressed up as a number.
    for column in (
        "PR",
        "Score",
        "Class",
        "Type",
        "Subsystem",
        "Size",
        "Impl LOC",
        "Test LOC",
        "Instr.",
        "Status",
        "Rejection",
    ):
        table.add_column(column)
    for candidate in candidates:
        assessment = candidate.assessment
        score = (
            f"{assessment.candidate_score:.2f}"
            if assessment.candidate_score is not None
            else "—"
        )
        candidate_class = (
            assessment.candidate_class.short_label
            if assessment.candidate_class is not None
            else "—"
        )
        table.add_row(
            str(candidate.pr.number),
            score,
            candidate_class,
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


def _fmt_score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "—"


def render_candidate_inspect(candidate: CandidateInfo) -> None:
    """`candidate inspect <PR>` (PRD §36, §71): the full decomposed assessment —
    every score line shows its component, and every fragment comes from stored
    fields (tier, linked issue, test counts, subsystem); '—' marks what was
    never measured, so the decision stays explainable and honest. The
    Recommendation branches on the STORED candidate_class — it was classified
    with the configured thresholds at mining time, and re-deriving thresholds in
    a renderer would duplicate that policy."""
    a = candidate.assessment
    echo(f"PR #{candidate.pr.number}")
    echo("")
    echo("Candidate Score")
    if a.candidate_score is None:
        echo("—")
        echo("Legacy hard_caps mining — re-run `repobench analyze` with")
        echo("size_policy: adaptive for component scores.")
    else:
        version = a.assessment_version or "unknown"
        klass = a.candidate_class.value if a.candidate_class else "unscored"
        echo(f"{a.candidate_score:.2f} · {klass} · {version}")
    echo("")
    tier_detail = f"tier {a.instruction_confidence}"
    if candidate.pr.linked_issue is not None:
        tier_detail += f" — linked issue #{candidate.pr.linked_issue.number}"
    kv("Instruction", f"{_fmt_score(a.instruction_score)}   {tier_detail}")
    verifier_detail = ""
    if a.test_files or a.test_loc:
        verifier_detail = f"   {a.test_files} test files · {a.test_loc} test LOC"
    kv("Verifier", f"{_fmt_score(a.verifier_score)}{verifier_detail}")
    kv("Reconstructability", _fmt_score(a.reconstructability_score))
    isolation_detail = f"   {a.subsystem}" if a.isolation_score is not None else ""
    kv("Isolation", f"{_fmt_score(a.isolation_score)}{isolation_detail}")
    kv("Environment", _fmt_score(a.environment_score))
    kv("Scope", _fmt_score(a.scope_score))
    echo("")
    echo("Change size")
    parts = [f"{a.implementation_loc:,} implementation LOC · {a.implementation_files} files"]
    if a.scope_percentile is not None:
        parts.append(f"P{a.scope_percentile:.0f} for this repository")
    echo(" · ".join(parts))
    if a.generated_loc or a.behavioral_loc is not None:
        echo(
            f"Generated LOC {a.generated_loc or 0}"
            + (f" · behavioral {a.behavioral_loc:,}" if a.behavioral_loc is not None else "")
        )
    if a.risk_flags:
        echo("")
        echo("Risk flags")
        echo(", ".join(a.risk_flags))
    echo("")
    echo("Classification")
    echo(a.complexity.value.upper())
    if a.candidate_class is not None:
        if (
            a.candidate_class is CandidateClass.HIGH_CONFIDENCE
            and a.complexity in (Complexity.LARGE, Complexity.XLARGE)
        ):
            recommendation = "Strong candidate despite large diff."
        elif a.candidate_class is CandidateClass.EXPERIMENTAL:
            recommendation = "Experimental — validate only after better candidates."
        else:
            recommendation = "Reasonable candidate."
        echo("")
        echo("Recommendation")
        echo(recommendation)


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
