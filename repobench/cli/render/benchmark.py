"""Benchmark build/refresh renderers (PRD §88; issues #15/#16, PRD §148)."""

from __future__ import annotations

from repobench.cli.render.base import WARN, echo, kv


def render_benchmark_build(outcome) -> None:  # noqa: ANN001 - BenchmarkBuildOutcome
    echo("")
    kv("Valid candidates", str(len(outcome.valid)))
    if getattr(outcome, "reuse_valid", False):
        # Incremental builds only (issue #16): how many tasks skipped revalidation.
        kv("Reused valid tasks", str(outcome.reused))
    kv("Requested benchmark size", str(outcome.requested_size))
    echo("")
    if len(outcome.valid) < outcome.requested_size:
        echo(
            f"note: only {len(outcome.valid)} valid task(s) available "
            f"(requested {outcome.requested_size}); the benchmark uses all of them."
        )
        echo("")
    # Adaptive insufficiency warnings (PRD §64): why the benchmark is smaller
    # than requested — always derived from what actually stopped the loop.
    for warning in getattr(outcome, "build_warnings", []):
        echo(f"{WARN} {warning}")
    if getattr(outcome, "build_warnings", []):
        echo("")
    echo("Benchmark")
    echo(outcome.benchmark_id)
    echo("")
    kv("Tasks", str(len(outcome.sample)))
    if getattr(outcome, "instruction_tiers", None):
        tiers = " ".join(
            f"{tier}×{count}"
            for tier, count in sorted(outcome.instruction_tiers.items())
        )
        kv("Instruction tiers", tiers)
    echo("")
    kv("Health", str(outcome.health.overall))
    echo("")
    kv("Representativeness", str(outcome.health.representativeness))
    kv("Validation", str(outcome.health.validation_confidence))
    # Issue #19: None means the persisted health predates the component — no
    # invented score (renderers must handle it).
    verifier = outcome.health.verifier_strength
    kv("Verifier strength", "—" if verifier is None else str(verifier))
    kv("Leakage", str(outcome.health.leakage_resistance))
    kv("Recency", str(outcome.health.recency))
    kv("Diversity", str(outcome.health.diversity))
    echo("")
    echo("Warnings")
    echo("")
    for warning in outcome.health.warnings:
        echo(f"{WARN} {warning}")
    echo("")
    echo(f"manifest: {outcome.manifest_path}")


def render_benchmark_refresh(outcome) -> None:  # noqa: ANN001 - RefreshOutcome
    """`benchmark refresh` (issue #15, PRD §148): the drift story first, then
    the task turnover of the rebuild. The Reason block only appears when a
    derived reason exists — nothing is invented."""
    drift = outcome.drift
    delta = drift.overall_after - drift.overall_before
    coverage_line = (
        f"Coverage: {drift.overall_before} → {drift.overall_after} ({delta:+d})"
    )
    echo("Benchmark refresh")
    echo("")
    echo(f"{outcome.old_benchmark_id} → {outcome.new_benchmark_id}")
    echo("")
    echo("Drift")
    echo("")
    if drift.drifted:
        echo(coverage_line)
    else:
        echo(f"{coverage_line} — benchmark still representative")
    dims = drift.per_dimension
    echo(
        f"task_type {dims['task_type'][0]} → {dims['task_type'][1]}"
        f" · subsystem {dims['subsystem'][0]} → {dims['subsystem'][1]}"
        f" · complexity {dims['complexity'][0]} → {dims['complexity'][1]}"
    )
    if drift.reasons:
        echo("Reason:")
        for reason in drift.reasons:
            echo(reason)
    echo("")
    tasks_line = (
        f"Tasks: {outcome.new_tasks} new · {outcome.retained_tasks} retained"
        f" · {len(outcome.build.sample)} total"
    )
    if outcome.missing_tasks:
        tasks_line += f" · {outcome.missing_tasks} missing"
    echo(tasks_line)
    if getattr(outcome.build, "reuse_valid", False):
        # Incremental refresh only (issue #16): how many tasks skipped revalidation.
        kv("Reused valid tasks", str(outcome.build.reused))
    echo(f"manifest: {outcome.new_manifest_path}")
