"""Data models for rendered reports (PRD §111-112)."""

from __future__ import annotations

from datetime import datetime

import pydantic

from repobench.analysis.metrics import SegmentStat, TargetMetrics
from repobench.analysis.pareto import ParetoResult
from repobench.analysis.recommendation import Recommendation
from repobench.analysis.reliability import TargetReliability
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


# Display cap for the distinct tampered-path list (issue #18): the section must
# stay readable when a rogue target rewrites the whole test suite.
TAMPERED_PATHS_CAP = 10


class TestTamperingStats(pydantic.BaseModel):
    """Reward-hacking signal across a run's trials (issue #18): trials whose
    final diff touches test files. A finding, never a verdict — tampered-but-
    passing trials stay SOLVED (PRD §42: verifiers define correctness)."""

    # not a pytest test class, despite the name
    __test__ = False

    flagged_trials: int
    total_trials: int
    # flagged trial count per target
    by_target: dict[str, int]
    # executed trial count per target, so the renderer can show "0/3" lines
    trials_by_target: dict[str, int]
    # tampered paths per flagged target, each list capped at TAMPERED_PATHS_CAP
    paths_by_target: dict[str, list[str]]
    # distinct tampered paths across the run, capped at TAMPERED_PATHS_CAP
    paths: list[str]


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
    # Per-target multi-rollout reliability (issue #13); None for runs that only
    # ever used a single rollout per task.
    reliability: dict[str, TargetReliability] | None = None
    # Reward-hacking signal (issue #18); None unless at least one trial's final
    # diff touched test files — same gating pattern as reliability.
    test_tampering: TestTamperingStats | None = None
    warnings: list[str]
    concurrency: int | None
    # Stored bootstrap seed (PRD §104: "com seed armazenada") so the run's
    # comparisons can be re-derived identically.
    bootstrap_seed: int | None = None
    generated_at: datetime = pydantic.Field(default_factory=utcnow)
