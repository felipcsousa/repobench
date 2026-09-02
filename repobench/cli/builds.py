"""Benchmark construction: `benchmark build` and `benchmark refresh` (issues #15-16).

Composes the pure benchmark domain (validation, sampling, coverage, drift, reuse)
with storage and the mining layer. Extraction keeps cli.services.py focused on
init/analyze/run; this module owns everything between candidates and a frozen
benchmark manifest.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from repobench.benchmark.coverage import CoverageReport, coverage_report
from repobench.benchmark.drift import DriftReport, compute_drift
from repobench.benchmark.health import HealthReport, compute_health
from repobench.benchmark.manifest import (
    build_manifest,
    load_stored_manifest,
    save_manifest,
)
from repobench.benchmark.reuse import (
    reusable_task_ids,
    reused_validation_report,
    task_id_for,
)
from repobench.benchmark.sampling import greedy_stratified_sample
from repobench.cli.reports import load_task_metadata
from repobench.cli.services import (
    CONFIG_FILENAME,
    ProjectPaths,
    analyze_repository,
    persist_candidates,
    project_paths,
    repository_visibility,
    resolve_benchmark,
    validate_targets,
)
from repobench.config import RepoBenchConfig
from repobench.core.errors import ReconstructionError, UsageError
from repobench.core.types import (
    CandidateInfo,
    ExecutionTarget,
    RejectionCode,
    TaskMetadata,
    TaskPackage,
    TaskStatus,
)
from repobench.execution.adapters.registry import get_adapter
from repobench.repository.git import GitRepo
from repobench.repository.workload import build_workload
from repobench.storage.db import Storage
from repobench.tasks.generation import generate_instruction
from repobench.tasks.instruction import render_instruction
from repobench.tasks.leakage import LeakageReport, scan_base_archive
from repobench.tasks.reconstruction import build_task_package
from repobench.validation.brittle import (
    BRITTLE_FINDINGS_CAP,
    brittle_assertions,
    brittle_file_warnings,
)
from repobench.validation.flakiness import flakiness_from_history
from repobench.validation.pipeline import (
    LEAKAGE_SCORE_THRESHOLD,
    TaskValidator,
    TaskValidationReport,
)

ProgressFn = Callable[[str], None]


# ------------------------------------------------------------ benchmark build


@dataclass
class TaskBuildResult:
    candidate: CandidateInfo
    package: TaskPackage
    leakage: LeakageReport
    report: TaskValidationReport

    @property
    def status(self) -> TaskStatus:
        return self.report.status

    @property
    def rejection_code(self) -> RejectionCode | None:
        return self.report.rejection_code

    @property
    def checks_passed(self) -> int:
        return sum(1 for c in self.report.checks if c.passed is True)

    @property
    def checks_total(self) -> int:
        return sum(1 for c in self.report.checks if c.passed is not None)


@dataclass
class BenchmarkBuildOutcome:
    benchmark_id: str
    manifest_path: Path
    requested_size: int
    valid: list[TaskBuildResult]
    rejected: list[TaskBuildResult]
    sample: list[TaskMetadata]
    coverage: CoverageReport
    health: HealthReport
    instruction_tiers: dict[str, int] = field(default_factory=dict)
    # Incremental build (issue #16): reuse_valid mirrors the CLI flag (after
    # --force-revalidate wins), reused counts tasks that skipped revalidation.
    reuse_valid: bool = False
    reused: int = 0
    # Issue #33: candidates whose history could not be reconstructed at all —
    # persisted REJECTED, but never in results (a TaskBuildResult needs a package).
    reconstruction_rejected: list[CandidateInfo] = field(default_factory=list)


def _package_for(repo: GitRepo, candidate: CandidateInfo, paths: ProjectPaths) -> TaskPackage:
    return build_task_package(repo.root, candidate, paths.task_dir(task_id_for(candidate)))


def _brittle_findings(verifier_diffs: list[str]) -> list[str]:
    """Brittle-assertion findings across this build's verifier diffs, capped
    (issue #19): the linter only points at files, so a bounded sample of
    findings is enough — never a score input."""
    findings: list[str] = []
    for diff in verifier_diffs:
        findings.extend(brittle_assertions(diff))
        if len(findings) >= BRITTLE_FINDINGS_CAP:
            break
    return findings[:BRITTLE_FINDINGS_CAP]


def _verifier_diff_text(package: TaskPackage) -> str:
    """The verifier patch text, or "" when unreadable — a missing diff is no
    finding, never an error (heuristic input only)."""
    try:
        return package.verifier_patch.read_text()
    except OSError:
        return ""


def _resolve_generation_target(cfg: RepoBenchConfig) -> ExecutionTarget | None:
    """The tier-D generation target when enabled, else None (UsageError when misconfigured)."""
    gen_cfg = cfg.instruction_generation
    if not gen_cfg.enabled:
        return None
    if gen_cfg.target not in cfg.targets:
        configured = ", ".join(sorted(cfg.targets)) or "none"
        raise UsageError(
            "instruction_generation.enabled requires instruction_generation.target "
            f"to reference a configured target in {CONFIG_FILENAME}; got "
            f"{gen_cfg.target!r} (configured: {configured})"
        )
    target = cfg.targets[gen_cfg.target]
    validate_targets([target])  # structural only — fails fast on a broken command template
    return target


def _apply_instruction_generation(
    repo: GitRepo,
    candidate: CandidateInfo,
    package: TaskPackage,
    target: ExecutionTarget,
    cfg: RepoBenchConfig,
    harness_version: str | None,
    log: ProgressFn | None,
) -> CandidateInfo:
    """Tier-D generation for candidates whose instruction is title-derived (source
    None or "title"); A/B candidates already carry pre-solution intent.

    Success: the candidate is upgraded to confidence D / source "llm" and the
    package's instruction.md + metadata.json are rewritten (metadata gains a
    `generation` extras block) so `_persist_validation` stores the new assessment.
    Failure (spawn/validator): the title-derived instruction is kept as-is, a
    `generation_failed` extras block records the reason, and validation proceeds.
    """
    if candidate.assessment.instruction_source not in (None, "title"):
        return candidate

    outcome = generate_instruction(
        candidate,
        package,
        target,
        cfg=cfg.instruction_generation,
        harness_version=harness_version,
    )

    if outcome.text is None:
        package.metadata.generation_failed = {
            "reason": outcome.failed_reason,
            "violations": outcome.violations,
            "attempts": outcome.attempts,
        }
        package.metadata_json.write_text(package.metadata.model_dump_json(indent=2))
        if log is not None:
            reason = outcome.failed_reason or "; ".join(outcome.violations) or "unknown"
            log(
                f"PR #{candidate.pr.number}: instruction generation failed ({reason}); "
                "keeping the title-derived instruction"
            )
        return candidate

    assessment = candidate.assessment.model_copy(
        update={
            "instruction": outcome.text,
            "instruction_confidence": "D",
            "instruction_source": "llm",
        }
    )
    candidate = candidate.model_copy(update={"assessment": assessment})
    package.metadata.assessment = assessment
    package.metadata.generation = {**outcome.metadata, "attempts": outcome.attempts}
    package.instruction_md.write_text(
        render_instruction(candidate, repo_name=repo.root.name)
    )
    package.metadata_json.write_text(package.metadata.model_dump_json(indent=2))
    return candidate


def _persist_validation(
    storage: Storage, candidate: CandidateInfo, result: TaskBuildResult
) -> None:
    package = result.package
    storage.save_task(
        package.task_id,
        data=package.metadata.model_dump(mode="json"),
        candidate_id=candidate.candidate_id,
        status=result.status.value,
        package_path=str(package.directory),
    )
    for check in result.report.checks:
        outcome = {True: "passed", False: "failed", None: "skipped"}[check.passed]
        storage.save_validation(package.task_id, check.name, outcome, check.details or None)
    storage.save_candidate(
        candidate.model_copy(
            update={"status": result.status, "rejection_code": result.rejection_code}
        )
    )


def build_benchmark(
    root: Path,
    cfg: RepoBenchConfig,
    storage: Storage,
    *,
    size: int | None = None,
    reuse_valid: bool = False,
    log: ProgressFn | None = None,
) -> BenchmarkBuildOutcome:
    """Validate candidate tasks, sample a representative benchmark (PRD §88-89, §126).

    With reuse_valid (issue #16) candidates whose deterministic task_id already
    validated VALID and whose package still loads skip the five historical
    validation checks; the leakage scan still runs and still gates."""
    paths = project_paths(root)
    repo = GitRepo(root)
    candidates = storage.list_candidates()
    pool = [c for c in candidates if c.status is not TaskStatus.FILTERED]
    if not pool:
        raise UsageError(
            "no task candidates to validate — run `repobench analyze` first"
        )

    # Tier pool filter (generalizes the old confidence-C knob): an explicit
    # allowed_confidences list restricts the pool BEFORE any validation work.
    allowed_confidences = cfg.benchmark.allowed_confidences
    if allowed_confidences is not None:
        pool = [
            c for c in pool if c.assessment.instruction_confidence in allowed_confidences
        ]
        if not pool:
            raise UsageError(
                "no task candidates match benchmark.allowed_confidences "
                f"{allowed_confidences} — nothing to benchmark"
            )

    # Tier-D generation (opt-in): spends real tokens, so it only ever runs here
    # in `benchmark build` — analyze stays token-free (PRD §10).
    generation_target = _resolve_generation_target(cfg)
    harness_version: str | None = None
    if generation_target is not None:
        harness_version = get_adapter(generation_target.harness).version()
        if log is not None:
            log(
                f"instruction generation enabled (target: {generation_target.id}, "
                f"harness: {generation_target.harness})"
            )

    validator = TaskValidator(cfg.project, workspaces_root=paths.workspaces_dir)
    # Incremental build (issue #16): the whole reuse decision is made here, once —
    # the loop below carries exactly one branch on it, never scattered conditionals.
    reusable = reusable_task_ids(storage, paths) if reuse_valid else set()
    reused_count = 0
    results: list[TaskBuildResult] = []
    reconstruction_rejected: list[CandidateInfo] = []
    for candidate in pool:
        try:
            package = _package_for(repo, candidate, paths)
        except ReconstructionError as exc:
            # Issue #33: one unreconstructable history (e.g. a PR based on the
            # empty-tree initial commit) rejects only itself — the build continues
            # with the remaining candidates. No package exists yet, so unlike
            # `_persist_validation` only the candidate row is persisted.
            rejected = candidate.model_copy(
                update={
                    "status": TaskStatus.REJECTED,
                    "rejection_code": RejectionCode.HISTORY_UNSUPPORTED,
                }
            )
            storage.save_candidate(rejected)
            reconstruction_rejected.append(rejected)
            if log is not None:
                log(
                    f"PR #{candidate.pr.number}: REJECTED (HISTORY_UNSUPPORTED) — "
                    f"{exc}"
                )
            continue
        if generation_target is not None:
            candidate = _apply_instruction_generation(
                repo,
                candidate,
                package,
                generation_target,
                cfg,
                harness_version,
                log,
            )
        leakage = scan_base_archive(package.metadata, package.base_tar)
        # Reuse gate: skip validation only when the flag is on, the task holds
        # status VALID from a previous build, its package loads, and leakage
        # still clears the threshold — anything else falls through to the full
        # validator, whose semantics (LEAKAGE_HIGH short-circuit) are unchanged.
        was_reused = (
            reuse_valid
            and package.task_id in reusable
            and leakage.score >= LEAKAGE_SCORE_THRESHOLD
        )
        if was_reused:
            report = reused_validation_report(package.task_id)
            reused_count += 1
        else:
            report = validator.validate(package, leakages=leakage)
        result = TaskBuildResult(
            candidate=candidate, package=package, leakage=leakage, report=report
        )
        package.metadata.status = report.status
        package.metadata.rejection_code = report.rejection_code
        package.metadata_json.write_text(package.metadata.model_dump_json(indent=2))
        _persist_validation(storage, candidate, result)
        results.append(result)
        if log is not None:
            pr = candidate.pr.number
            if was_reused:
                log(f"PR #{pr}: {result.status.value} (reused)")
            else:
                code = f" ({result.rejection_code.value})" if result.rejection_code else ""
                log(f"PR #{pr}: {result.status.value}{code}")

    valid = [r for r in results if r.status is TaskStatus.VALID]
    if not valid:
        codes = sorted(
            {
                *(r.rejection_code.value for r in results if r.rejection_code),
                *(
                    c.rejection_code.value
                    for c in reconstruction_rejected
                    if c.rejection_code
                ),
            }
        )
        raise UsageError(
            "no valid tasks were produced — nothing to benchmark. "
            f"Rejection codes: {', '.join(codes) or 'unknown'}. "
            "Check project.test_command in repobench.yml and `repobench candidates`."
        )

    requested = size if size is not None else cfg.benchmark.size
    metadatas = [r.package.metadata for r in valid]
    sample = greedy_stratified_sample(metadatas, requested, cfg.benchmark.dimensions)

    universe = build_workload(candidates)  # the full Workload Universe (PRD §66)
    coverage = coverage_report(universe, sample, cfg.benchmark.dimensions)

    total_checks = sum(r.checks_total for r in results)
    passed_ratio = (
        sum(r.checks_passed for r in results) / total_checks if total_checks else 0.0
    )
    sample_ids = {meta.task_id for meta in sample}
    sampled_valid = [r for r in valid if r.package.task_id in sample_ids]
    leakage_score = (
        round(statistics.mean(r.leakage.score for r in sampled_valid))
        if sampled_valid
        else 0
    )
    universe_counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate.assessment.task_type.value
        universe_counts[key] = universe_counts.get(key, 0) + 1

    # Verifier strength (issue #19): flakiness comes from the append-only
    # validation history via Storage's single reader, scoped to the tasks this
    # build actually validated; brittle assertions are scanned from the
    # sampled tasks' verifier patches and only ever warn, never score.
    build_task_ids = {r.package.task_id for r in results}
    history = [
        row for row in storage.validation_history() if row["task_id"] in build_task_ids
    ]
    flakiness = flakiness_from_history(history)
    brittle_warnings = brittle_file_warnings(
        _brittle_findings([_verifier_diff_text(r.package) for r in sampled_valid])
    )

    health = compute_health(
        coverage=coverage,
        all_checks_passed_ratio=passed_ratio,
        leakage_score=leakage_score,
        tasks=sample,
        universe_counts=universe_counts,
        lookback_days=cfg.repository.lookback_days,
        public_repository=repository_visibility(repo.remote_slug) == "PUBLIC",
        flaky_tasks=flakiness.flaky_tasks,
        total_validated_tasks=flakiness.tasks_seen,
        brittle_warnings=brittle_warnings,
    )
    if log is not None:
        # One line each (issue #19): the full detail travels in health.warnings.
        if flakiness.flaky_tasks:
            log(
                f"{len(flakiness.flaky_tasks)} task(s) with flaky validation "
                "history (outcome flipped between builds)"
            )
        for warning in brittle_warnings:
            log(warning)

    manifest = build_manifest(
        sample,
        health,
        coverage,
        cfg.benchmark,
        repository=repo.remote_slug,
    )
    manifest_path = save_manifest(manifest, paths.benchmark_dir(manifest.benchmark_id))
    storage.save_benchmark(
        manifest.benchmark_id,
        size=len(sample),
        health_json=health.model_dump_json(),
        manifest_path=str(manifest_path),
        methodology_version=manifest.methodology_version,
    )
    for position, meta in enumerate(sample):
        storage.save_benchmark_task(manifest.benchmark_id, meta.task_id, position)

    # Instruction tier mix of the sample (PRD §71-72): D tasks are derived from
    # the solution by construction, so their presence is always called out.
    instruction_tiers: dict[str, int] = {}
    for meta in sample:
        tier = meta.assessment.instruction_confidence
        instruction_tiers[tier] = instruction_tiers.get(tier, 0) + 1
    if instruction_tiers and log is not None:
        tier_line = " ".join(
            f"{tier}×{count}" for tier, count in sorted(instruction_tiers.items())
        )
        log(f"Instruction tiers: {tier_line}")

    return BenchmarkBuildOutcome(
        benchmark_id=manifest.benchmark_id,
        manifest_path=manifest_path,
        requested_size=requested,
        valid=valid,
        rejected=[r for r in results if r.status is not TaskStatus.VALID],
        sample=sample,
        coverage=coverage,
        health=health,
        instruction_tiers=instruction_tiers,
        reuse_valid=reuse_valid,
        reused=reused_count,
        reconstruction_rejected=reconstruction_rejected,
    )


# ---------------------------------------------------------- benchmark refresh


@dataclass
class RefreshOutcome:
    """Everything `benchmark refresh` renders (issue #15, PRD §148)."""

    old_benchmark_id: str
    new_benchmark_id: str
    drift: DriftReport
    new_tasks: int
    retained_tasks: int
    missing_tasks: int
    new_manifest_path: Path
    build: BenchmarkBuildOutcome


def refresh_benchmark(
    root: Path,
    cfg: RepoBenchConfig,
    storage: Storage,
    *,
    benchmark_id: str | None = None,
    size: int | None = None,
    reuse_valid: bool = False,
    force_revalidate: bool = False,
    log: ProgressFn | None = None,
) -> RefreshOutcome:
    """Re-mine the repo, measure the stored benchmark's drift and rebuild (issue
    #15, PRD §148). Composition only: analyze/persist + coverage over the OLD
    sample against the NEW universe + build_benchmark; the old benchmark and its
    manifest are never touched (a rebuild produces a new id, PRD §89)."""
    row = resolve_benchmark(storage, benchmark_id)
    old_id = row["benchmark_id"]
    manifest = load_stored_manifest(row)
    if manifest is None:
        raise UsageError(
            f"benchmark {old_id} has no loadable manifest — cannot measure drift"
        )
    if manifest.coverage is None:
        raise UsageError(
            f"benchmark {old_id} predates coverage tracking — rebuild it with "
            "`repobench benchmark build` to measure drift"
        )
    analyzed = analyze_repository(root, cfg)
    persist_candidates(storage, analyzed.candidates)

    # Old sample metadata may be incomplete when packages/rows were removed;
    # gaps are reported, never fatal (issue #15).
    old_sample = load_task_metadata(project_paths(root), storage, manifest.task_ids)
    missing_tasks = len(manifest.task_ids) - len(old_sample)
    universe = build_workload(analyzed.candidates)
    after = coverage_report(
        universe, list(old_sample.values()), cfg.benchmark.dimensions
    )
    drift = compute_drift(manifest.coverage, after, universe, list(old_sample.values()))

    build = build_benchmark(
        root,
        cfg,
        storage,
        size=size or manifest.size,
        reuse_valid=reuse_valid and not force_revalidate,
        log=log,
    )
    new_ids = [meta.task_id for meta in build.sample]
    old_ids = set(manifest.task_ids)
    return RefreshOutcome(
        old_benchmark_id=old_id,
        new_benchmark_id=build.benchmark_id,
        drift=drift,
        new_tasks=sum(1 for tid in new_ids if tid not in old_ids),
        retained_tasks=sum(1 for tid in new_ids if tid in old_ids),
        missing_tasks=missing_tasks,
        new_manifest_path=build.manifest_path,
        build=build,
    )
