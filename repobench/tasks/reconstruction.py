"""Task package reconstruction from repository history (PRD §36, §64, §71-73).

Materializes BASE (`git archive`), splits the PR diff into gold (implementation)
and hidden verifier patches, and writes a complete task package.
"""

from __future__ import annotations

from pathlib import Path

from repobench.core import gitutil
from repobench.core.ids import new_task_id
from repobench.core.types import CandidateInfo, TaskMetadata, TaskPackage, TaskStatus
from repobench.tasks.instruction import render_instruction
from repobench.tasks.package import write_package
from repobench.tasks.verifier import split_diff


def build_task_package(repo: Path, candidate: CandidateInfo, out_dir: Path) -> TaskPackage:
    """Build a full task package for `candidate` into `out_dir`.

    Raises RuntimeError when the history is not reconstructable (missing SHAs or
    `git archive` failure).
    """
    repo = Path(repo)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pr = candidate.pr
    base_sha = pr.base_sha
    gold_sha = pr.merge_sha
    if not base_sha or not gold_sha:
        raise RuntimeError(
            f"candidate {candidate.candidate_id} has no base/merge SHA; "
            "history not reconstructable"
        )

    base_tar = out_dir / "base.tar"
    if not gitutil.archive_commit(repo, base_sha, base_tar):
        raise RuntimeError(
            f"git archive of {base_sha} failed in {repo}; history not reconstructable"
        )

    diff = gitutil.diff_commits(repo, base_sha, gold_sha)
    split = split_diff(diff)

    metadata = TaskMetadata(
        task_id=new_task_id(pr.number, base_sha, gold_sha),
        pr_number=pr.number,
        title=pr.title,
        base_sha=base_sha,
        gold_sha=gold_sha,
        assessment=candidate.assessment,
        created_at=pr.merged_at,
        status=TaskStatus.VALIDATING,
        rejection_code=None,
        package_dir=str(out_dir),
    )

    return write_package(
        out_dir,
        base_tar=base_tar,
        instruction=render_instruction(candidate, repo_name=repo.name),
        gold_patch=split.implementation_patch,
        verifier_patch=split.verifier_patch,
        metadata=metadata,
    )
