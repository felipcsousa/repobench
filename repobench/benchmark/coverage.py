"""Benchmark coverage scores: how close the sample is to the Workload Universe (PRD §85)."""

from __future__ import annotations

import pydantic

from repobench.benchmark.sampling import distribution_of, tv_distance
from repobench.config import BenchmarkDimensions
from repobench.core.types import TaskMetadata, WorkloadDistribution


class CoverageReport(pydantic.BaseModel):
    """Per-dimension coverage scores (0-100) plus weighted overall (PRD §85)."""

    task_type: int
    subsystem: int
    complexity: int
    overall: int


_DIMENSIONS = ("task_type", "subsystem", "complexity")


def coverage_report(
    universe: WorkloadDistribution,
    sample: list[TaskMetadata],
    weights: BenchmarkDimensions,
) -> CoverageReport:
    """Score each dimension 100 - TV*100; overall is the weighted mean with normalized weights."""
    sample_dists = {
        "task_type": distribution_of([t.assessment.task_type.value for t in sample]),
        "subsystem": distribution_of([t.assessment.subsystem for t in sample]),
        "complexity": distribution_of([t.assessment.complexity.value for t in sample]),
    }
    universe_dists = {
        "task_type": universe.task_type,
        "subsystem": universe.subsystem,
        "complexity": universe.complexity,
    }
    dim_weights = {
        "task_type": weights.task_type,
        "subsystem": weights.subsystem,
        "complexity": weights.complexity,
    }
    total_weight = sum(dim_weights.values())

    scores: dict[str, int] = {}
    for dim in _DIMENSIONS:
        tv = tv_distance(sample_dists[dim], universe_dists[dim])
        scores[dim] = max(0, 100 - round(tv * 100))

    weight = lambda dim: dim_weights[dim] / total_weight if total_weight > 0 else 1 / 3
    overall = round(sum(scores[dim] * weight(dim) for dim in _DIMENSIONS))
    return CoverageReport(
        task_type=scores["task_type"],
        subsystem=scores["subsystem"],
        complexity=scores["complexity"],
        overall=overall,
    )
