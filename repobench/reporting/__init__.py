"""Report rendering: terminal text and machine-readable JSON (PRD §111-112)."""

from repobench.reporting.export import render_csv, render_jsonl
from repobench.reporting.json_report import render_json
from repobench.reporting.models import (
    InstructionGenerationStats,
    PairComparison,
    ReportData,
)
from repobench.reporting.terminal import render_report

__all__ = [
    "InstructionGenerationStats",
    "PairComparison",
    "ReportData",
    "render_csv",
    "render_json",
    "render_jsonl",
    "render_report",
]
