"""Instruction provenance (PRD §71-72).

The instruction must come from intent recorded before/with the change — linked issue,
PR problem statement, or PR title. Never reverse-engineered from the gold diff.

Confidence:
  A — pre-existing issue
  B — strong PR problem description
  C — potentially solution-contaminated description (title, or PR body with fix details)
  D — LLM-derived from the implementation diff (opt-in; see
      repobench/tasks/generation.py). Ranked BELOW C: derived from the solution
      by construction, the anti-solution prompt/validator mitigate but do not
      eliminate this. This module never yields D — it only covers tiers A/B/C.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from repobench.core.types import PRInfo

_PR_BODY_MIN_CHARS = 80
_ISSUE_BODY_MAX_CHARS = 4000

# Phrases that suggest the body describes the fix rather than the problem (PRD §72).
_CONTAMINATION_PHRASES = ("fixed by", "the fix", "solution is")
_CHANGED_TO_RE = re.compile(r"changed\s+.+\s+to\b", re.IGNORECASE)


class InstructionResult(BaseModel):
    text: str
    source: str
    confidence: Literal["A", "B", "C"]


def _looks_contaminated(body: str) -> bool:
    if "```" in body:
        return True
    lowered = body.lower()
    return (
        any(phrase in lowered for phrase in _CONTAMINATION_PHRASES)
        or bool(_CHANGED_TO_RE.search(lowered))
    )


def derive_instruction(pr: PRInfo) -> InstructionResult | None:
    """Best available instruction text for a PR, or None when provenance is absent."""
    issue = pr.linked_issue
    if issue is not None:
        text = f"{issue.title}\n\n{(issue.body or '')[:_ISSUE_BODY_MAX_CHARS]}".strip()
        if text:
            return InstructionResult(text=text, source="issue", confidence="A")

    body = (pr.body or "").strip()
    if len(body) >= _PR_BODY_MIN_CHARS:
        confidence = "C" if _looks_contaminated(body) else "B"
        return InstructionResult(text=body, source="pr_body", confidence=confidence)

    title = (pr.title or "").strip()
    if title:
        return InstructionResult(text=title, source="title", confidence="C")

    return None
