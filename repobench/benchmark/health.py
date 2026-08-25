"""Benchmark Health calculation."""

from __future__ import annotations

from datetime import UTC, datetime

from repobench.logging import get_logger
from repobench.models import BenchmarkHealth, CandidateTask, NetworkIsolation

log = get_logger("benchmark.health")

# Weights from the PRD
_W_REPRESENTATIVENESS = 0.40
_W_VALIDATION = 0.20
_W_LEAKAGE = 0.15
_W_RECENCY = 0.15
_W_DIVERSITY = 0.10


def calculate_health(
    benchmark_tasks: list[CandidateTask],
    workload_universe: list[CandidateTask] | None = None,
) -> BenchmarkHealth:
    """Compute the Benchmark Health score components.

    Components (0-100 each):
    - Representativeness: TVD-based coverage of workload distributions
    - Validation: no-op/oracle/determinism validation strength
    - Leakage: history sanitization, network isolation, credential removal
    - Recency: temporal distribution of tasks
    - Diversity: repetition of subsystem/complexity

    Overall = weighted sum.
    """
    health = BenchmarkHealth()

    if not benchmark_tasks:
        return health

    health.representativeness = _representativeness(benchmark_tasks, workload_universe)
    health.validation = _validation(benchmark_tasks)
    health.leakage = _leakage(benchmark_tasks)
    health.recency = _recency(benchmark_tasks)
    health.diversity = _diversity(benchmark_tasks)

    health.overall = round(
        _W_REPRESENTATIVENESS * health.representativeness
        + _W_VALIDATION * health.validation
        + _W_LEAKAGE * health.leakage
        + _W_RECENCY * health.recency
        + _W_DIVERSITY * health.diversity
    )

    return health


def _representativeness(
    tasks: list[CandidateTask],
    workload: list[CandidateTask] | None,
) -> int:
    """Representativeness from distribution coverage (TVD-based)."""
    if not workload:
        return 100

    from repobench.benchmark.coverage import calculate_coverage

    # Build distributions
    def _dist(tasks_list: list[CandidateTask], key: str) -> dict[str, float]:
        from collections import Counter

        counts = Counter(getattr(t, key) for t in tasks_list)
        total = sum(counts.values())
        if total == 0:
            return {}
        return {k: v / total for k, v in counts.items()}

    bench_dist = {
        "task_type": _dist(tasks, "task_type"),
        "subsystem": _dist(tasks, "subsystem"),
        "complexity": _dist(tasks, "complexity"),
    }
    work_dist = {
        "task_type": _dist(workload, "task_type"),
        "subsystem": _dist(workload, "subsystem"),
        "complexity": _dist(workload, "complexity"),
    }

    coverage = calculate_coverage(bench_dist, work_dist)
    return round(sum(coverage.values()) / max(len(coverage), 1))


def _validation(tasks: list[CandidateTask]) -> int:
    """Validation confidence from eligibility flags."""
    if not tasks:
        return 0

    scores = []
    for t in tasks:
        score = 100
        e = t.eligibility

        # No-op validated (must fail)
        if getattr(e, "noop", None) is not True:
            score -= 30
        # Oracle validated (must pass)
        if getattr(e, "oracle", None) is not True:
            score -= 30
        # Regression validated
        if getattr(e, "regression", None) is not True:
            score -= 10
        # Determinism validated
        if getattr(e, "determinism", None) is not True:
            score -= 20
        # Instruction provenance: Tier C reduces confidence
        prov = getattr(t, "instruction_provenance", None)
        if prov is not None and prov.value == "C":
            score -= 15

        scores.append(max(0, min(score, 100)))

    return round(sum(scores) / len(scores))


def _leakage(tasks: list[CandidateTask]) -> int:
    """Leakage resistance score."""
    if not tasks:
        return 0

    scores = []
    for t in tasks:
        score = 100
        # Network isolation
        net = getattr(t, "network_isolation", NetworkIsolation.NONE)
        if net == NetworkIsolation.NONE:
            score -= 25
        elif net == NetworkIsolation.PARTIAL:
            score -= 10

        # Leakage risk from scanner
        risk = getattr(t, "leakage_risk", 0.0)
        score -= round(risk * 40)

        # History eligibility (sanitized snapshot implies history available)
        e = getattr(t, "eligibility", None)
        if e is not None and getattr(e, "leakage", None) is True:
            pass  # history sanitized
        elif e is not None and getattr(e, "leakage", None) is False:
            score -= 30

        scores.append(max(0, min(score, 100)))

    return round(sum(scores) / len(scores))


def _recency(tasks: list[CandidateTask]) -> int:
    """Recency score: how recent are the tasks (0-100)."""
    now = datetime.now(UTC)
    scores = []
    for t in tasks:
        created = getattr(t, "created_at", None)
        if created is None:
            scores.append(50)
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_days = max(0, (now - created).days)
        # 180-day window: recency decays linearly
        score = max(0, 100 - age_days * 100 / 180)
        scores.append(score)

    if not scores:
        return 0
    return round(sum(scores) / len(scores))


def _diversity(tasks: list[CandidateTask]) -> int:
    """Diversity: penalize repeated subsystem/complexity combinations."""
    if not tasks:
        return 0

    from collections import Counter

    combos = Counter(
        (t.subsystem or "unknown", t.complexity.value if t.complexity else "unknown") for t in tasks
    )
    total = len(tasks)
    max_repeat = max(combos.values())

    # Ideal: all distinct (max_repeat=1). Score drops as max_repeat grows.
    score = max(0, 100 - (max_repeat - 1) * 100 / max(total, 1))
    return round(score)
