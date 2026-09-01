"""Greedy stratified sampling of a representative benchmark (PRD §83-84)."""

from __future__ import annotations

from repobench.config import BenchmarkDimensions
from repobench.core.types import TaskMetadata


def distribution_of(values: list[str]) -> dict[str, float]:
    """Normalized share (0-1) per key; empty input gives an empty distribution."""
    total = len(values)
    if total == 0:
        return {}
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {key: count / total for key, count in counts.items()}


def tv_distance(sample_dist: dict[str, float], universe_dist: dict[str, float]) -> float:
    """Total-variation distance over the union of keys (PRD §84).

    Constraint: an empty sample is maximally unrepresentative — distance 1 by convention.
    """
    if not sample_dist:
        return 1.0
    keys = set(sample_dist) | set(universe_dist)
    return 0.5 * sum(
        abs(sample_dist.get(key, 0.0) - universe_dist.get(key, 0.0)) for key in keys
    )


def _dimension_values(task: TaskMetadata) -> tuple[str, str, str]:
    a = task.assessment
    return (a.task_type.value, a.subsystem, a.complexity.value)


# Index order of the dimensions in _dimension_values / dim_weights (PRD §66).
DIMENSION_NAMES = ("task_type", "subsystem", "complexity")


def greedy_stratified_sample(
    tasks: list[TaskMetadata],
    size: int,
    weights: BenchmarkDimensions,
) -> list[TaskMetadata]:
    """Greedy stratified selection minimizing weighted TV distance to the universe (PRD §84).

    Fully deterministic: ties are broken by task_id lexicographic order, so repeated
    calls always return the same sample for the same inputs.
    """
    if size <= 0 or not tasks:
        return []
    if size >= len(tasks):
        return list(tasks)

    universe_dists = [
        distribution_of([_dimension_values(t)[i] for t in tasks])
        for i in range(len(DIMENSION_NAMES))
    ]
    dim_weights = (weights.task_type, weights.subsystem, weights.complexity)

    sample: list[TaskMetadata] = []
    sample_values: list[tuple[str, str, str]] = []
    chosen_ids: set[str] = set()

    while len(sample) < size:
        best_task: TaskMetadata | None = None
        best_key: tuple[float, str] | None = None
        for candidate in tasks:
            if candidate.task_id in chosen_ids:
                continue
            candidate_values = _dimension_values(candidate)
            weighted = 0.0
            for i, weight in enumerate(dim_weights):
                trial_dist = distribution_of(
                    [vals[i] for vals in sample_values] + [candidate_values[i]]
                )
                weighted += weight * tv_distance(trial_dist, universe_dists[i])
            key = (weighted, candidate.task_id)
            if best_key is None or key < best_key:
                best_key = key
                best_task = candidate
        # size <= len(tasks) guarantees a candidate is always found.
        assert best_task is not None
        sample.append(best_task)
        sample_values.append(_dimension_values(best_task))
        chosen_ids.add(best_task.task_id)

    return sample
