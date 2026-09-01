"""Workload statistics over mined candidates (PRD §66, §10, §126).

Pure functions — no I/O. The benchmark sample must represent the Workload Universe,
so these distributions are what sampling tries to match.
"""

from __future__ import annotations

from repobench.core.types import AnalyzeSummary, CandidateInfo, TaskStatus, WorkloadDistribution


def distribution(values: list[str]) -> dict[str, float]:
    """Normalized shares (0-1) in first-occurrence order; empty for empty input."""
    if not values:
        return {}
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = len(values)
    return {key: count / total for key, count in counts.items()}


def build_workload(candidates: list[CandidateInfo]) -> WorkloadDistribution:
    """Distributions over task type, subsystem and complexity (PRD §66)."""
    return WorkloadDistribution(
        task_type=distribution([c.assessment.task_type.value for c in candidates]),
        subsystem=distribution([c.assessment.subsystem for c in candidates]),
        complexity=distribution([c.assessment.complexity.value for c in candidates]),
    )


def suggest_benchmark_size(n_candidates: int) -> int:
    """PRD §126: 15-30 tasks by default when available; never invents tasks."""
    return min(n_candidates, 30)


def summarize_analysis(
    total_merged_prs: int, candidates: list[CandidateInfo], suggested_size: int
) -> AnalyzeSummary:
    """The `repobench analyze` summary (PRD §10): universe size, candidates, workload."""
    return AnalyzeSummary(
        total_merged_prs=total_merged_prs,
        task_candidates=len(candidates),
        validated_candidates=sum(
            1 for candidate in candidates if candidate.status == TaskStatus.DISCOVERED
        ),
        workload=build_workload(candidates),
        suggested_benchmark_size=suggested_size,
    )
