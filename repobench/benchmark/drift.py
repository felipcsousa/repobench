"""Benchmark drift detection (issue #15, PRD §148).

`benchmark refresh` re-mines the Workload Universe and asks whether the stored
benchmark still represents it: coverage is recomputed against the FRESH universe
while the benchmark's sample stays fixed. ANY drop in overall coverage marks the
benchmark as drifted — there is no threshold, because the printed numbers carry
the magnitude; a rise or hold means the benchmark still matches the workload.

Reasons are derived, never invented: only segments whose universe share exceeds
the current sample share are named, in the dimension that dropped the most.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from repobench.benchmark.coverage import CoverageReport
from repobench.benchmark.sampling import distribution_of
from repobench.core.types import TaskMetadata, WorkloadDistribution

# The exact dimensions coverage_report scores; the order also breaks ties when
# picking the dimension with the biggest drop.
DRIFT_DIMENSIONS = ("task_type", "subsystem", "complexity")


def _sample_dimension_values(dimension: str, sample: list[TaskMetadata]) -> list[str]:
    """Segment values of the sample for one dimension — the same classification
    coverage_report uses (task_type value / subsystem / complexity value)."""
    if dimension == "task_type":
        return [task.assessment.task_type.value for task in sample]
    if dimension == "subsystem":
        return [task.assessment.subsystem for task in sample]
    return [task.assessment.complexity.value for task in sample]


@dataclass
class DriftReport:
    """Stored → fresh coverage plus the derived drift story (issue #15, PRD §148).

    per_dimension maps each of task_type/subsystem/complexity to (before, after);
    reasons phrase the underrepresented segments; drifted is ANY overall drop.
    """

    overall_before: int
    overall_after: int
    per_dimension: dict[str, tuple[int, int]]
    reasons: list[str] = field(default_factory=list)
    drifted: bool = False


def compute_drift(
    before: CoverageReport,
    after: CoverageReport,
    universe: WorkloadDistribution,
    sample: list[TaskMetadata],
) -> DriftReport:
    """Compare stored coverage against the fresh universe (issue #15, PRD §148).

    In the dimension with the biggest drop, up to 2 segments whose universe
    share exceeds the CURRENT sample share by the largest margin are named.
    A rise or hold yields drifted=False and no reasons; a drop whose
    biggest-dropping dimension has no underrepresented segment yields reasons=[].
    """
    per_dimension = {
        dim: (getattr(before, dim), getattr(after, dim)) for dim in DRIFT_DIMENSIONS
    }
    drifted = after.overall < before.overall
    reasons: list[str] = []
    if drifted and sample:
        biggest = min(
            DRIFT_DIMENSIONS,
            key=lambda dim: (
                per_dimension[dim][1] - per_dimension[dim][0],
                DRIFT_DIMENSIONS.index(dim),
            ),
        )
        universe_shares = getattr(universe, biggest)
        sample_shares = distribution_of(_sample_dimension_values(biggest, sample))
        ranked = sorted(
            (
                (universe_shares[segment] - sample_shares.get(segment, 0.0), segment)
                for segment in universe_shares
            ),
            key=lambda item: (-item[0], item[1]),
        )
        reasons = [
            f"{segment} work increased and is underrepresented in the benchmark"
            for margin, segment in ranked[:2]
            if margin > 0
        ]
    return DriftReport(
        overall_before=before.overall,
        overall_after=after.overall,
        per_dimension=per_dimension,
        reasons=reasons,
        drifted=drifted,
    )
