"""Machine-readable JSON report (PRD §112). Module named json_report to avoid
shadowing the stdlib json module."""

from __future__ import annotations

from repobench.reporting.models import ReportData


def render_json(data: ReportData) -> str:
    return data.model_dump_json(indent=2)
