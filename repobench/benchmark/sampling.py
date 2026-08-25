"""Representative benchmark sampling via greedy stratified optimization."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from repobench.config import RepoBenchConfig
from repobench.logging import get_logger
from repobench.models import CandidateTask, TaskStatus

log = get_logger("benchmark.sampling")


@dataclass
class _Dist:
    """A categorical distribution for one dimension."""

    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def prob(self, key: str) -> float:
        if self.total == 0:
            return 0.0
        return self.counts.get(key, 0) / self.total

    def add(self, key: str) -> None:
        self.counts[key] += 1


def workload_distribution(
    workload: list[CandidateTask],
) -> tuple[_Dist, _Dist, _Dist]:
    """Build task_type, subsystem, and complexity distributions."""
    type_dist = _Dist()
    sub_dist = _Dist()
    comp_dist = _Dist()

    for task in workload:
        type_dist.add(task.task_type.value)
        sub_dist.add(task.subsystem or "unknown")
        comp_dist.add(task.complexity.value)

    return type_dist, sub_dist, comp_dist


def select_benchmark(
    candidates: list[CandidateTask],
    workload: list[CandidateTask],
    config: RepoBenchConfig,
) -> list[CandidateTask]:
    """Select a representative sample from VALID candidates.

    Implements greedy stratified optimization: for each slot, pick the
    candidate that most reduces the TVD between the benchmark distribution
    and the workload distribution, with a diversity penalty.

    Returns a list of selected candidates (up to config.benchmark.size).
    """
    valid = [c for c in candidates if c.status == TaskStatus.VALID]
    if not valid:
        log.warning("No VALID candidates available for sampling")
        return []

    size = config.benchmark.size
    if size <= 0:
        size = 24

    size = min(size, len(valid))
    weights = config.benchmark.dimensions

    # Workload distributions (target)
    w_type, w_sub, w_comp = workload_distribution(workload or valid)

    # Benchmark distributions (current selection)
    b_type = _Dist()
    b_sub = _Dist()
    b_comp = _Dist()

    selected: list[CandidateTask] = []
    available = list(valid)

    for _ in range(size):
        best_candidate = None
        best_score = float("inf")

        for cand in available:
            # Simulate inclusion
            sim_type = _Dist(b_type.counts.copy())
            sim_sub = _Dist(b_sub.counts.copy())
            sim_comp = _Dist(b_comp.counts.copy())
            sim_type.add(cand.task_type.value)
            sim_sub.add(cand.subsystem or "unknown")
            sim_comp.add(cand.complexity.value)

            # Total variation distance per dimension
            tvd_type = _tvd(sim_type, w_type)
            tvd_sub = _tvd(sim_sub, w_sub)
            tvd_comp = _tvd(sim_comp, w_comp)

            score = (
                weights.task_type * tvd_type
                + weights.subsystem * tvd_sub
                + weights.complexity * tvd_comp
            )

            # Diversity penalty: penalize candidates too similar to selected
            penalty = _diversity_penalty(cand, selected)
            score += penalty

            if score < best_score:
                best_score = score
                best_candidate = cand

        if best_candidate is None:
            break

        selected.append(best_candidate)
        available.remove(best_candidate)
        b_type.add(best_candidate.task_type.value)
        b_sub.add(best_candidate.subsystem or "unknown")
        b_comp.add(best_candidate.complexity.value)

    log.info("Selected %d tasks for benchmark (requested %d)", len(selected), size)
    return selected


def _tvd(bench: _Dist, workload: _Dist) -> float:
    """Total Variation Distance between two categorical distributions."""
    keys = set(bench.counts.keys()) | set(workload.counts.keys())
    return 0.5 * sum(abs(bench.prob(k) - workload.prob(k)) for k in keys)


def _diversity_penalty(candidate: CandidateTask, selected: list[CandidateTask]) -> float:
    """Penalize candidates that are too similar to already-selected ones."""
    if not selected:
        return 0.0

    penalty = 0.0
    for sel in selected:
        # Same PR family or same subsystem + complexity => high similarity
        if sel.subsystem == candidate.subsystem and sel.complexity == candidate.complexity:
            penalty += 0.10
        if sel.task_type == candidate.task_type and sel.subsystem == candidate.subsystem:
            penalty += 0.08
        if sel.subsystem == candidate.subsystem:
            penalty += 0.04

    return penalty
