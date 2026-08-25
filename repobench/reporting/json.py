"""JSON report export."""

from __future__ import annotations

import json
from pathlib import Path

from repobench.logging import get_logger
from repobench.models import Report

log = get_logger("reporting.json")


def export_json(report: Report, output_path: Path) -> Path:
    """Export a report to JSON format.

    Returns the output path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = _report_to_dict(report)
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    log.info("JSON report written to %s", output_path)
    return output_path


def report_to_dict(report: Report) -> dict:
    """Convert a Report to a JSON-serializable dict."""
    return _report_to_dict(report)


def _report_to_dict(report: Report) -> dict:
    """Convert a Report to a JSON-serializable dict."""
    config_metrics = {}
    for name, m in report.config_metrics.items():
        config_metrics[name] = m.model_dump()

    segments = []
    for seg in report.segments:
        segments.append(
            {
                "category": seg.category,
                "metrics": {k: v.model_dump() for k, v in seg.metrics.items()},
            }
        )

    return {
        "benchmark_id": report.benchmark_id,
        "repository": report.repository,
        "tasks": report.tasks,
        "health": report.health.model_dump(),
        "configs": config_metrics,
        "comparisons": [c.model_dump() for c in report.comparisons],
        "segments": segments,
        "recommendation": report.recommendation,
        "recommendation_reason": report.recommendation_reason,
        "warnings": report.warnings,
        "public_repo_warning": report.public_repo_warning,
    }
