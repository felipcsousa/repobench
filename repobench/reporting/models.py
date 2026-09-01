"""Data models for rendered reports (PRD §111-112)."""

from __future__ import annotations

from datetime import datetime

import pydantic

from repobench.analysis.metrics import SegmentStat, TargetMetrics
from repobench.analysis.pareto import ParetoResult
from repobench.analysis.recommendation import Recommendation
from repobench.benchmark.health import HealthReport
from repobench.core.types import utcnow


class PairComparison(pydantic.BaseModel):
    """Paired comparison of two targets on the same tasks (PRD §104-105)."""

    target_a: str
    target_b: str
    diff_pp: float
    ci_lo_pp: float
    ci_hi_pp: float
    conclusive: bool


class InstructionGenerationStats(pydantic.BaseModel):
    """Tier-D generation outcome across the benchmark's tasks (PRD §71-72):
    D tasks are solution-derived by construction, so the fallback rate matters."""

    generated: int
    failed: int

    @property
    def total(self) -> int:
        return self.generated + self.failed


class ReportData(pydantic.BaseModel):
    """Everything a report needs; machine-readable via model_dump_json (PRD §112)."""

    benchmark_id: str | None
    repository: str | None
    run_id: str | None
    tasks_total: int
    health: HealthReport | None
    targets: list[TargetMetrics]
    comparisons: list[PairComparison]
    recommendation: Recommendation | None
    # Non-dominated targets on quality × cost (or quality × time) (PRD §106)
    pareto: ParetoResult | None = None
    # dimension -> segment -> target -> stat (PRD §109)
    segments: dict[str, dict[str, dict[str, SegmentStat]]]
    # Tier-D instruction generation success/fallback across the benchmark
    instruction_generation: InstructionGenerationStats | None = None
    warnings: list[str]
    concurrency: int | None
    # Stored bootstrap seed (PRD §104: "com seed armazenada") so the run's
    # comparisons can be re-derived identically.
    bootstrap_seed: int | None = None
    generated_at: datetime = pydantic.Field(default_factory=utcnow)
