"""Multi-rollout reliability estimators: pass@k and pass^k (issue #13, PRD §103).

pass@k uses the unbiased estimator 1 - C(n-c, k) / C(n, k) (Chen et al., 2021)
and is only defined for tasks with at least k rollouts — tasks with fewer stored
rollouts are skipped, never invented into the estimate. pass^k ("pass-hat") is
the honest reliability figure per the issue: the fraction of tasks where every
stored rollout solved.

Consistency: with k=1 and one trial per task both reduce to the aggregate solve
rate. Cost follows the analysis/metrics.py honesty rules (PRD §53-54) — a cost
figure exists only when every counted trial reported one.
"""

from __future__ import annotations

import math

import pydantic

from repobench.core.types import TrialOutcome, TrialResult


class TargetReliability(pydantic.BaseModel):
    """Reliability of one target at k rollouts per task (issue #13, PRD §103)."""

    target_id: str
    k: int
    n_tasks: int
    pass_at_k: float
    pass_hat_k: float
    all_solved_tasks: int
    per_task_variance: float
    cost_per_reliable_solve_usd: float | None


def reliability_stats(trials: list[TrialResult], k: int) -> dict[str, TargetReliability]:
    """Per-target pass@k / pass^k over multi-rollout trials (issue #13).

    Trials group by (target_id, task_id); n_i = the task's stored rollout count
    and c_i = its solved count. pass@k averages the unbiased estimator over the
    tasks with n_i >= k only; pass^k and all_solved_tasks count a task only when
    every stored rollout solved (retried attempts included — one failed rollout
    breaks the all-k claim). per_task_variance is the population variance of the
    per-task solve rates across the counted tasks (0.0 for a single task).
    """
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    by_task: dict[str, dict[str, list[TrialResult]]] = {}
    for trial in trials:
        by_task.setdefault(trial.target_id, {}).setdefault(trial.task_id, []).append(trial)

    result: dict[str, TargetReliability] = {}
    for target_id in sorted(by_task):
        eligible = [
            task_trials
            for task_trials in by_task[target_id].values()
            if len(task_trials) >= k
        ]
        n_tasks = len(eligible)
        pass_at_k = 0.0
        all_solved = 0
        rates: list[float] = []
        counted: list[TrialResult] = []
        for task_trials in eligible:
            n_i = len(task_trials)
            c_i = sum(1 for t in task_trials if t.outcome is TrialOutcome.SOLVED)
            rates.append(c_i / n_i)
            pass_at_k += 1.0 - math.comb(n_i - c_i, k) / math.comb(n_i, k)
            if c_i == n_i:
                all_solved += 1
            counted.extend(task_trials)

        if n_tasks:
            pass_at_k /= n_tasks
        pass_hat_k = all_solved / n_tasks if n_tasks else 0.0
        mean_rate = sum(rates) / n_tasks if n_tasks else 0.0
        variance = (
            sum((rate - mean_rate) ** 2 for rate in rates) / n_tasks
            if n_tasks > 1
            else 0.0
        )

        # Cost honesty (PRD §53-54): only when every counted trial reported one.
        total_cost: float | None = None
        if counted and all(t.cost_usd is not None for t in counted):
            total_cost = sum(t.cost_usd for t in counted)
        cost_per_reliable = (
            total_cost / all_solved if total_cost is not None and all_solved > 0 else None
        )

        result[target_id] = TargetReliability(
            target_id=target_id,
            k=k,
            n_tasks=n_tasks,
            pass_at_k=pass_at_k,
            pass_hat_k=pass_hat_k,
            all_solved_tasks=all_solved,
            per_task_variance=variance,
            cost_per_reliable_solve_usd=cost_per_reliable,
        )
    return result
