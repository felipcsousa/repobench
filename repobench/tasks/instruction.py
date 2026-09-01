"""Instruction rendering (PRD §71-72).

Builds instruction.md from candidate metadata. Pure string building — the
rendering step involves no LLM. The instruction text itself normally comes
from the repository history (issue body / PR problem statement) assessed by
mining; with opt-in tier-D generation (repobench/tasks/generation.py) it may
have been drafted from the gold implementation diff during benchmark build.
"""

from __future__ import annotations

from repobench.core.types import CandidateInfo


def render_instruction(candidate: CandidateInfo, *, repo_name: str | None = None) -> str:
    """Render the instruction.md content for a candidate.

    Header line with the PR title, the raw instruction text (issue body / PR
    problem statement), and a short Context note (repository, task type, subsystem).
    """
    pr = candidate.pr
    title = (pr.title or "").strip() or f"PR #{pr.number}"
    body = candidate.assessment.instruction.strip() or pr.body.strip() or title

    lines = [
        f"# {title}",
        "",
        body,
        "",
        "## Context",
        "",
        f"- Repository: {repo_name or 'unknown'}",
        f"- Task type: {candidate.assessment.task_type.value}",
        f"- Subsystem: {candidate.assessment.subsystem}",
    ]
    return "\n".join(lines) + "\n"
