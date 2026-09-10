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
from repobench.benchmark.queue import (
    TerminationReason,
    UniverseProfile,
    strategy_for,
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
    # How the validation loop ended (PRD §61-66): one explicit fact from which
    # the historical outcome flags below are derived.
    termination: TerminationReason = field(
        default_factory=lambda: TerminationReason(
            reason="pool_exhausted", validated_count=0, valid_count=0, remaining=0
        )
    )

    # ---- historical outcome flags, derived from `termination` --------------

    @property
    def insufficient(self) -> bool:
        """Adaptive build (PRD §64): the loop ended with fewer VALID tasks
        than requested — the benchmark is still built, honest, smaller."""
        return self.termination.insufficient(self.requested_size)

    @property
    def build_warnings(self) -> list[str]:
        """Human-readable warnings that explain an undersized build; empty
        list = no insufficiency."""
        return self.termination.warnings(self.requested_size)

    @property
    def budget_stopped_at(self) -> int | None:
        """PRD §63: how many candidates the validation budget allowed before
        the loop stopped; None = the loop was never budget-limited."""
        return (
            self.termination.validated_count
            if self.termination.reason == "budget"
            else None
        )


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
    stop_when_sufficient: bool = False,
    log: ProgressFn | None = None,
) -> BenchmarkBuildOutcome:
    """Validate candidate tasks, sample a representative benchmark (PRD §88-89, §126).

    With reuse_valid (issue #16) candidates whose deterministic task_id already
    validated VALID and whose package still loads skip the five historical
    validation checks; the leakage scan still runs and still gates.

    With stop_when_sufficient the loop breaks as soon as enough VALID tasks
    cover the requested size — cheaper, but the sampling pool is smaller, so
    distribution matching weakens (opt-in; the default validates everything).

    Adaptive builds (task_mining.size_policy: adaptive, PRD §25-29, §61-66) turn
    that opt-in into the strategy: the pool is class-filtered
    (task_mining.validation.include, default high+medium), candidates are
    validated in coverage-aware priority order (HIGH → MEDIUM → EXPERIMENTAL,
    then 0.65×score + 0.35×coverage need, re-picked after every validation),
    the loop stops at requested size + min_representativeness, and
    benchmark.validation_budget.max_candidates bounds the work. hard_caps keeps
    today's semantics unchanged."""
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

    # The validation strategy (PRD §25): the mode-specific half of the loop —
    # pool filter, pick order, stop condition — lives in the strategy, so the
    # loop below carries no branching on the mining mode. Adaptive also
    # class-filters the pool here (PRD §33).
    requested = size if size is not None else cfg.benchmark.size
    strategy = strategy_for(cfg, requested, stop_when_sufficient)
    pool = strategy.filter_pool(pool, candidates)

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
    if log is not None:
        # Cost warning before the slow step: each fresh candidate pays a full
        # validation pass (install + every check), which is minutes of work.
        warning = (
            f"validating {len(pool)} candidate(s) — each fresh one runs install + "
            "baseline/verifier/gold/regression checks; this is the slow step"
        )
        if reuse_valid:
            warning += (
                " (candidates already VALID from a previous build are reused, "
                "not revalidated)"
            )
        log(warning)
    results: list[TaskBuildResult] = []
    reconstruction_rejected: list[CandidateInfo] = []
    # Queue state (PRD §25-29, §63): the strategy picks the next candidate
    # after every validation so coverage need can steer the adaptive order
    # dynamically; hard_caps keeps today's fixed pr_number order. The universe
    # profile is computed once — the distributions are invariant for the whole
    # build. The budget counts every candidate that paid validation work
    # (fresh or reused).
    profile = UniverseProfile.of(candidates, cfg.benchmark.dimensions)
    remaining = list(pool)
    valid_assessments: list = []
    budget = cfg.benchmark.validation_budget.max_candidates
    validated_count = 0
    valid_count = 0
    position = 0
    termination: TerminationReason | None = None
    while remaining:
        candidate = strategy.pick(remaining, valid_assessments, profile)
        remaining.remove(candidate)
        position += 1
        if log is not None:
            log(
                f"[{position}/{len(pool)}] PR #{candidate.pr.number}"
                f"{strategy.describe(candidate)}: validating…"
            )
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
        # gold_patch feeds the identifier needles (PRD §44): identifiers the
        # gold introduces and that the base mentions are HIGH evidence.
        leakage = scan_base_archive(
            package.metadata, package.base_tar, gold_patch=package.gold_patch
        )
        # Reuse gate: skip validation only when the flag is on, the task holds
        # status VALID from a previous build, its package loads, and leakage
        # still clears — risk not HIGH (PRD §45; MEDIUM warns, LOW is clean) —
        # anything else falls through to the full validator, whose semantics
        # (LEAKAGE_HIGH short-circuit) are unchanged.
        was_reused = (
            reuse_valid
            and package.task_id in reusable
            and leakage.risk != "HIGH"
        )
        if was_reused:
            report = reused_validation_report(package.task_id)
            reused_count += 1
        else:
            report = validator.validate(package, leakages=leakage)
        validated_count += 1
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
        if result.status is TaskStatus.VALID:
            valid_assessments.append(result.candidate.assessment)
            if log is not None:
                # PRD §28/§60: the adaptive strategy announces the queue
                # steering only when the top coverage gap actually moves —
                # never per pick.
                line = strategy.note_reprioritization(
                    valid_assessments, profile, remaining
                )
                if line is not None:
                    log(line)
        valid_count = sum(1 for r in results if r.status is TaskStatus.VALID)
        stop = strategy.should_stop(valid_count, valid_assessments, profile, remaining)
        if stop is not None:
            if log is not None:
                log(stop.message)
            termination = TerminationReason(
                reason=stop.reason,
                validated_count=validated_count,
                valid_count=valid_count,
                remaining=len(remaining),
            )
            break
        if budget is not None and validated_count >= budget and remaining:
            # PRD §63: bounded work — the budget, not the pool, ended the loop.
            if log is not None:
                log(
                    f"validation budget reached: {validated_count} candidates "
                    f"validated ({valid_count} valid so far), {len(remaining)} "
                    "left unvalidated"
                )
            termination = TerminationReason(
                reason="budget",
                validated_count=validated_count,
                valid_count=valid_count,
                remaining=len(remaining),
            )
            break
    if termination is None:
        termination = TerminationReason(
            reason="pool_exhausted",
            validated_count=validated_count,
            valid_count=valid_count,
            remaining=len(remaining),
        )

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
            "Check project.test_command in repobench.yml and `repobench candidates`; "
            "`repobench candidates --show <PR>` prints each task's validation "
            "checks with output tails (issue #35)."
        )

    # Graceful insufficiency (PRD §64-66): below benchmark.min_size there is no
    # honest benchmark to build (INSUFFICIENT_BENCHMARK, no strong claims);
    # between min_size and the requested size the build proceeds — smaller and
    # honestly warned, which beats no benchmark at all. Adaptive strategies
    # only: legacy hard_caps builds keep their historical "build with what
    # validated" behavior.
    if strategy.enforces_min_size and len(valid) < cfg.benchmark.min_size:
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
            f"INSUFFICIENT_BENCHMARK — only {len(valid)} VALID task(s), below the "
            f"minimum of {cfg.benchmark.min_size} (benchmark.min_size). "
            f"Rejection codes: {', '.join(codes) or 'unknown'}. "
            "Widen repository.lookback_days, relax task_mining.validation.include, "
            "or raise benchmark.validation_budget.max_candidates."
        )
    if len(valid) < requested:
        if log is not None:
            log(
                f"requested {requested}, available {len(valid)} — building with "
                f"{len(valid)} tasks"
            )

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
        termination=termination,
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
    stop_when_sufficient: bool = False,
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
        stop_when_sufficient=stop_when_sufficient,
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
