"""Plain-text terminal report renderer (PRD §111).

Constraint: pure f-strings, no Rich dependency — the CLI styles the output;
this renderer must stay printable and testable as-is.
"""

from __future__ import annotations

from repobench.reporting.models import ReportData

_LOW_SAMPLE_NOTICE = "LOW SAMPLE — do not route decisions from this segment."


def _format_duration(ms: int | None) -> str:
    """Render milliseconds like the PRD examples: 9m18, 11m03, or 42s below a minute."""
    if ms is None:
        return "—"
    total_seconds = int(round(ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    return f"{minutes}m{seconds:02d}"


def _format_money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "—"


def _format_pp(value: float) -> str:
    return f"{value:+.0f}pp"


def render_report(data: ReportData) -> str:
    lines: list[str] = []

    lines.append("REPOBENCH")
    lines.append("")
    lines.append("Repository")
    lines.append(data.repository or "—")
    lines.append("")
    lines.append("Benchmark")
    lines.append(data.benchmark_id or "—")
    lines.append("")
    lines.append("Tasks")
    lines.append(str(data.tasks_total))
    if data.health is not None:
        lines.append("")
        lines.append("Benchmark Health")
        lines.append(f"{data.health.overall}/100")

    lines.append("")
    lines.append(f"{'Target':<24}{'Solve':>8}{'Time':>10}{'$/Solve':>10}")
    lines.append("")
    for metrics in data.targets:
        lines.append(
            f"{metrics.target_id:<24}"
            f"{metrics.solve_rate * 100:>7.0f}%"
            f"{_format_duration(metrics.time_p50_ms):>10}"
            f"{_format_money(metrics.cost_per_solve_usd):>10}"
        )

    if data.targets:
        lines.append("")
        lines.append("95% CI (Wilson) — solve rate")
        for metrics in data.targets:
            if metrics.wilson_lo is None or metrics.wilson_hi is None:
                continue  # never render an interval that was not computed
            lines.append(
                f"  {metrics.target_id}: "
                f"{metrics.wilson_lo * 100:.0f}%–{metrics.wilson_hi * 100:.0f}%"
            )

    for comparison in data.comparisons:
        lines.append("")
        lines.append(f"{comparison.target_a} vs {comparison.target_b}")
        lines.append("")
        lines.append(f"Observed difference: {_format_pp(comparison.diff_pp)}")
        lines.append(
            f"95% CI: {_format_pp(comparison.ci_lo_pp)} → {_format_pp(comparison.ci_hi_pp)}"
        )
        lines.append("")
        if comparison.conclusive:
            lines.append("Statistically conclusive difference.")
        else:
            lines.append("No conclusive quality difference.")

    if data.recommendation is not None:
        lines.append("")
        if data.recommendation.recommended:
            lines.append("Cost-effective recommendation:")
            lines.append(data.recommendation.recommended)
        else:
            lines.append(data.recommendation.reason)

    for dimension, segments in data.segments.items():
        lines.append("")
        lines.append(f"Segments — {dimension}")
        lines.append("")
        target_ids = sorted({t for stats in segments.values() for t in stats})
        lines.append(f"{'Segment':<16}" + "".join(f"{t:>12}" for t in target_ids))
        for segment in sorted(segments):
            stats = segments[segment]
            row = f"{segment:<16}"
            for target_id in target_ids:
                if target_id in stats:
                    row += f"{stats[target_id].rate * 100:>11.0f}%"
                else:
                    row += f"{'—':>12}"
            if any(stat.low_sample for stat in stats.values()):
                row += f"  {_LOW_SAMPLE_NOTICE}"
            lines.append(row)

    if data.warnings:
        lines.append("")
        lines.append("Benchmark warnings:")
        lines.append("")
        for warning in data.warnings:
            lines.append(f"⚠ {warning}")

    lines.append("")
    lines.append(f"Generated {data.generated_at.isoformat()}")
    if data.concurrency is not None:
        lines.append(f"Concurrency: {data.concurrency}")

    return "\n".join(lines)
