"""`repobench compare` renderer (PRD §149, issue #14)."""

from __future__ import annotations

from repobench.cli.render.base import WARN, echo


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
