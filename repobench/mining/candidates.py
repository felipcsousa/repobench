"""Candidate mining: merged PRs -> potential retrospective eval tasks (PRD §65-70, §82).

For every merged PR the diff is split into implementation vs test changes, instruction
provenance is derived, and hard filters produce a CandidateInfo with a stable rejection
code. Bot PRs are dropped entirely — a candidate must be a human-origin change.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from repobench.config import TaskMiningConfig
from repobench.core.gitutil import rev_parse
from repobench.core.ids import new_candidate_id
from repobench.core.testpaths import is_test_path, split_changed_paths
from repobench.core.types import (
    Assessment,
    CandidateInfo,
    PRInfo,
    RejectionCode,
    TaskStatus,
)
from repobench.mining.classification import classify_task_type
from repobench.mining.complexity import compute_complexity
from repobench.mining.instruction import derive_instruction
from repobench.mining.subsystem import infer_subsystem
from repobench.repository.git import GitRepo


def _first_segment(path: str) -> str:
    return path.replace("\\", "/").split("/")[0]


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


def mine_candidates(
    repo: GitRepo,
    cfg: TaskMiningConfig,
    *,
    enrich: Callable[[PRInfo], PRInfo | None] | None = None,
    lookback_days: int = 180,
    now: datetime | None = None,
) -> list[CandidateInfo]:
    """Mine one repository pass: every merged PR becomes a candidate or a filtered one."""
    candidates: list[CandidateInfo] = []
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

        # Binary files (-1 numstat) count as files but contribute no LOC.
        changes = repo.numstat(base_sha, merge_sha) if base_sha and merge_sha else []
        impl_paths, test_paths = split_changed_paths([path for _, _, path in changes])
        impl_loc = sum(
            max(added, 0) + max(removed, 0)
            for added, removed, path in changes
            if not is_test_path(path)
        )
        test_loc = sum(
            max(added, 0) + max(removed, 0)
            for added, removed, path in changes
            if is_test_path(path)
        )
        implementation_files = len(impl_paths)
        test_files = len(test_paths)
        packages_touched = len({_first_segment(path) for path in impl_paths if _first_segment(path)})

        task_type = classify_task_type(effective, changed_files)
        complexity = compute_complexity(impl_loc, implementation_files, packages_touched, cfg)
        subsystem = infer_subsystem(changed_files)
        instruction = derive_instruction(effective)

        # The assessment is built once per PR; every candidate outcome carries it.
        assessment = Assessment(
            task_type=task_type,
            subsystem=subsystem,
            complexity=complexity,
            implementation_loc=impl_loc,
            test_loc=test_loc,
            implementation_files=implementation_files,
            test_files=test_files,
            instruction=instruction.text if instruction else "",
            # Default confidence "C" is unused while instruction is empty (candidate filtered).
            instruction_confidence=instruction.confidence if instruction else "C",
            instruction_source=instruction.source if instruction else None,
        )

        def build(
            status: TaskStatus, rejection_code: RejectionCode | None
        ) -> CandidateInfo:
            return _make_candidate(
                effective,
                status,
                rejection_code,
                assessment=assessment,
            )

        # Hard filters in fixed order (PRD §70, §82) — first failure wins.
        if (
            not base_sha
            or not head_sha
            or not merge_sha
            or rev_parse(repo.root, base_sha) is None
            or rev_parse(repo.root, head_sha) is None
        ):
            candidates.append(build(TaskStatus.FILTERED, RejectionCode.HISTORY_UNSUPPORTED))
            continue
        if instruction is None:
            candidates.append(build(TaskStatus.FILTERED, RejectionCode.NO_INSTRUCTION))
            continue
        if cfg.require_test_change and (test_files == 0 or test_loc == 0):
            candidates.append(build(TaskStatus.FILTERED, RejectionCode.NO_TEST_CHANGE))
            continue
        if impl_loc == 0 or impl_loc < cfg.min_implementation_loc:
            candidates.append(build(TaskStatus.FILTERED, RejectionCode.TASK_TOO_SMALL))
            continue
        if impl_loc > cfg.max_implementation_loc or implementation_files > cfg.max_implementation_files:
            candidates.append(build(TaskStatus.FILTERED, RejectionCode.TASK_TOO_LARGE))
            continue
        candidates.append(build(TaskStatus.DISCOVERED, None))
    return candidates
