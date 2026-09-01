"""Report rendering: terminal text and machine-readable JSON (PRD §111-112)."""

from repobench.reporting.json_report import render_json
from repobench.reporting.models import PairComparison, ReportData
from repobench.reporting.terminal import render_report

__all__ = [
    "PairComparison",
    "ReportData",
    "render_json",
    "render_report",
]
