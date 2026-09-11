"""Candidate mining: merged PRs -> potential retrospective eval tasks (PRD §65-70, §82).

For every merged PR the diff is split into implementation vs test changes and
instruction provenance is derived. The configured size policy decides the
outcome (PRD §9-10, §33-34): "hard_caps" (default) keeps the legacy absolute
LOC/files limits authoritative; "adaptive" keeps only the methodological hard
gates and scores every surviving PR with the deterministic candidate
assessment (repobench/mining/assessment.py) instead of rejecting by size. Bot
PRs are dropped entirely — a candidate must be a human-origin change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from repobench.config import ProjectConfig, TaskMiningConfig
from repobench.core.gitutil import rev_parse
from repobench.core.ids import new_candidate_id
from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    Complexity,
    PRInfo,
    RejectionCode,
    TaskStatus,
    TaskType,
)
from repobench.mining.assessment import (
    ASSESSMENT_VERSION,
    ComponentScore,
    aggregate,
    assess_environment,
    assess_instruction,
    assess_isolation,
    assess_reconstructability,
    assess_scope,
    assess_verifier,
    classify,
    environment_unknown,
    risk_flags,
)
from repobench.mining.classification import classify_task_type
from repobench.mining.complexity import compute_complexity, compute_complexity_percentile
from repobench.mining.instruction import InstructionResult, derive_instruction
from repobench.mining.scope import (
    ScopeFeatures,
    behavioral_loc,
    percentile_rank,
    scope_features,
)
from repobench.mining.subsystem import infer_subsystem
from repobench.repository.git import GitRepo


@dataclass(frozen=True)
class _PrRecord:
    """Everything pass 1 gathers for one merged PR, before any status decision."""

    pr: PRInfo
    features: ScopeFeatures
    task_type: TaskType
    subsystem: str
    instruction: InstructionResult | None
    resolvable: bool


def _make_candidate(
    pr: PRInfo,
    status: TaskStatus,
    rejection_code: RejectionCode | None,
    *,
    assessment: Assessment,
) -> CandidateInfo:
    return CandidateInfo(
        candidate_id=new_candidate_id(pr.number, pr.base_sha or pr.merge_sha or ""),
        pr=pr,
        status=status,
        rejection_code=rejection_code,
        assessment=assessment,
    )


def _base_assessment(record: _PrRecord, complexity: Complexity) -> Assessment:
    """The original 11 mining fields (PRD §68): carried by every outcome;
    scored adaptive candidates extend these with the adaptive-v1 fields."""
    features = record.features
    instruction = record.instruction
    return Assessment(
        task_type=record.task_type,
        subsystem=record.subsystem,
        complexity=complexity,
        implementation_loc=features.implementation_loc,
        test_loc=features.test_loc,
        implementation_files=features.implementation_files,
        test_files=features.test_files,
        instruction=instruction.text if instruction else "",
        # Default confidence "C" is unused while instruction is empty (candidate filtered).
        instruction_confidence=instruction.confidence if instruction else "C",
        instruction_source=instruction.source if instruction else None,
    )


def _hard_caps_candidate(record: _PrRecord, cfg: TaskMiningConfig) -> CandidateInfo:
    """Legacy decision path (PRD §10, §34, §38): absolute gates in their
    historical order, first failure wins — byte-identical to pre-adaptive
    mining. No scores: assessment_version stays None (PRD §68 "not computed")."""
    features = record.features
    assessment = _base_assessment(
        record,
        compute_complexity(
            features.implementation_loc,
            features.implementation_files,
            features.packages_touched,
            cfg,
        ),
    )

    def gated(status: TaskStatus, rejection_code: RejectionCode | None) -> CandidateInfo:
        return _make_candidate(record.pr, status, rejection_code, assessment=assessment)

    if not record.resolvable:
        return gated(TaskStatus.FILTERED, RejectionCode.HISTORY_UNSUPPORTED)
    if record.instruction is None:
        return gated(TaskStatus.FILTERED, RejectionCode.NO_INSTRUCTION)
    if cfg.require_test_change and (features.test_files == 0 or features.test_loc == 0):
        return gated(TaskStatus.FILTERED, RejectionCode.NO_TEST_CHANGE)
    if features.implementation_loc == 0 or features.implementation_loc < cfg.min_implementation_loc:
        return gated(TaskStatus.FILTERED, RejectionCode.TASK_TOO_SMALL)
    if (
        features.implementation_loc > cfg.max_implementation_loc
        or features.implementation_files > cfg.max_implementation_files
    ):
        return gated(TaskStatus.FILTERED, RejectionCode.TASK_TOO_LARGE)
    return gated(TaskStatus.DISCOVERED, None)


def _adaptive_candidate(
    record: _PrRecord,
    cfg: TaskMiningConfig,
    baseline: list[float],
    project_cfg: ProjectConfig | None,
) -> CandidateInfo:
    """Adaptive decision path (PRD §9-10, §37-39): the methodological hard
    gates first, then a scored assessment. Size never rejects — a PR below
    min_implementation_loc is LOW_PRIORITY (not invalid, PRD §37), and an
    EXPERIMENTAL score also only deprioritizes."""
    features = record.features
    absolute_complexity = compute_complexity(
        features.implementation_loc,
        features.implementation_files,
        features.packages_touched,
        cfg,
    )

    def gated(status: TaskStatus, rejection_code: RejectionCode | None) -> CandidateInfo:
        return _make_candidate(
            record.pr,
            status,
            rejection_code,
            assessment=_base_assessment(record, absolute_complexity),
        )

    # Hard gates in fixed order (PRD §9, §37-39) — first failure wins.
    if not record.resolvable:
        return gated(TaskStatus.FILTERED, RejectionCode.HISTORY_UNSUPPORTED)
    if record.instruction is None:
        return gated(TaskStatus.FILTERED, RejectionCode.NO_INSTRUCTION)
    if cfg.require_test_change and (features.test_files == 0 or features.test_loc == 0):
        return gated(TaskStatus.FILTERED, RejectionCode.NO_TEST_CHANGE)
    if features.implementation_loc == 0:
        # Implementation must exist (PRD §9): zero implementation LOC changes
        # no implemented behavior, whatever the tests do.
        return gated(TaskStatus.FILTERED, RejectionCode.NO_IMPLEMENTATION_CHANGE)
    if (
        features.implementation_loc > cfg.extreme_scope_loc
        or features.implementation_files > cfg.extreme_scope_files
    ):
        # Extreme scope guard (PRD §39): the emergency cap for pathological
        # diffs, never a complexity threshold.
        return gated(TaskStatus.FILTERED, RejectionCode.TASK_EXTREME_SCOPE)
    if features.implementation_loc < cfg.min_implementation_loc:
        # Low priority is not invalidity (PRD §37): size no longer hard-rejects,
        # so no rejection code accompanies the status.
        return gated(TaskStatus.LOW_PRIORITY, None)

    # Scored candidate (PRD §11-24). The percentile is rounded once and reused
    # for complexity, scope confidence and risk flags, so every stored field
    # recomputes exactly from the stored percentile. None (no repository
    # baseline: nothing to rank against) keeps the absolute complexity and lets
    # assess_scope / risk_flags treat it as not measured — never an invented
    # neutral P50.
    behavioral = behavioral_loc(features)
    ranked = percentile_rank(behavioral, baseline)
    percentile = round(ranked, 1) if ranked is not None else None
    complexity = (
        compute_complexity_percentile(percentile)
        if cfg.complexity.method == "repository_percentile" and percentile is not None
        else absolute_complexity
    )
    components: dict[str, ComponentScore] = {
        "instruction": assess_instruction(record.instruction),
        "verifier": assess_verifier(features),
        "reconstructability": assess_reconstructability(
            record.pr.base_sha, record.pr.head_sha, record.pr.merge_sha, record.resolvable
        ),
        "scope": assess_scope(percentile),
        "isolation": assess_isolation(features, record.subsystem),
        "environment": (
            assess_environment(project_cfg) if project_cfg is not None else environment_unknown()
        ),
    }
    score = aggregate(components)
    candidate_class = classify(score, cfg)
    assessment = Assessment(
        task_type=record.task_type,
        subsystem=record.subsystem,
        complexity=complexity,
        implementation_loc=features.implementation_loc,
        test_loc=features.test_loc,
        implementation_files=features.implementation_files,
        test_files=features.test_files,
        instruction=record.instruction.text if record.instruction else "",
        instruction_confidence=record.instruction.confidence if record.instruction else "C",
        instruction_source=record.instruction.source if record.instruction else None,
        candidate_score=score,
        candidate_class=candidate_class,
        instruction_score=components["instruction"].score,
        verifier_score=components["verifier"].score,
        reconstructability_score=components["reconstructability"].score,
        scope_score=components["scope"].score,
        isolation_score=components["isolation"].score,
        environment_score=components["environment"].score,
        scope_percentile=percentile,
        generated_loc=features.generated_loc,
        behavioral_loc=behavioral,
        dirs_touched=features.dirs_touched,
        languages_touched=list(features.languages),
        risk_flags=risk_flags(features, percentile, len(baseline), features.test_loc),
        assessment_version=ASSESSMENT_VERSION,
    )
    # HIGH/MEDIUM enter the default validation pool; EXPERIMENTAL stays
    # eligible but low priority (PRD §24, §37).
    status = (
        TaskStatus.DISCOVERED
        if candidate_class in (CandidateClass.HIGH_CONFIDENCE, CandidateClass.MEDIUM_CONFIDENCE)
        else TaskStatus.LOW_PRIORITY
    )
    return _make_candidate(record.pr, status, None, assessment=assessment)


def mine_candidates(
    repo: GitRepo,
    cfg: TaskMiningConfig,
    *,
    enrich: Callable[[PRInfo], PRInfo | None] | None = None,
    lookback_days: int = 180,
    now: datetime | None = None,
    codeowners: dict[str, str] | None = None,
    package_dirs: dict[str, str] | None = None,
    project_cfg: ProjectConfig | None = None,
) -> list[CandidateInfo]:
    """Mine one repository pass: every merged PR becomes a candidate or a filtered one.

    `codeowners`/`package_dirs` feed subsystem inference (PRD §68); None falls
    back to the first-path-segment heuristic. `project_cfg` feeds the adaptive
    environment component (PRD §23); None scores it neutral instead of
    inventing signals.

    Two passes (PRD §9-10, §17-18): pass 1 gathers raw per-PR data; pass 2
    finalizes — under the default "hard_caps" policy the legacy absolute gates
    run unchanged; under "adaptive" only the methodological gates run and every
    surviving PR is scored, with scope normalized against the repository's own
    behavioral-LOC distribution.
    """
    records: list[_PrRecord] = []
    for pr in repo.merged_prs(lookback_days, now=now):
        effective = pr
        if enrich is not None:
            enriched = enrich(pr)
            if enriched is not None:  # an unusable enrichment result is ignored
                effective = enriched

        # Human-origin change required (PRD §70) — bot PRs never become candidates.
        if effective.is_bot:
            continue

        base_sha, head_sha, merge_sha = (
            effective.base_sha,
            effective.head_sha,
            effective.merge_sha,
        )
        changed_files = list(effective.changed_files)
        if not changed_files and base_sha and merge_sha:
            changed_files = repo.changed_files(base_sha, merge_sha)

        # Binary files (-1 numstat) count as files but contribute no LOC. The
        # scope features share the task splitter's test/implementation boundary
        # (PRD §73): every Assessment LOC/file figure below derives from them.
        changes = repo.numstat(base_sha, merge_sha) if base_sha and merge_sha else []
        features = scope_features(changes)
        resolvable = bool(
            base_sha
            and head_sha
            and merge_sha
            and rev_parse(repo.root, base_sha) is not None
            and rev_parse(repo.root, head_sha) is not None
        )

        records.append(
            _PrRecord(
                pr=effective,
                features=features,
                task_type=classify_task_type(effective, changed_files),
                subsystem=infer_subsystem(
                    changed_files, codeowners=codeowners, package_dirs=package_dirs
                ),
                instruction=derive_instruction(effective),
                resolvable=resolvable,
            )
        )

    # Adaptive scope baseline (PRD §17-18): behavioral LOC of every mined PR
    # that changed implementation code — the repository normalizes itself.
    baseline = [
        behavioral_loc(record.features)
        for record in records
        if record.features.implementation_loc > 0
    ]

    adaptive = cfg.size_policy == "adaptive"
    candidates: list[CandidateInfo] = []
    for record in records:  # pass-1 order kept: deterministic output (PRD §72)
        if adaptive:
            candidates.append(_adaptive_candidate(record, cfg, baseline, project_cfg))
        else:
            candidates.append(_hard_caps_candidate(record, cfg))
    return candidates
