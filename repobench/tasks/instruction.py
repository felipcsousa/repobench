"""Instruction extraction from PRs with provenance tracking."""

from __future__ import annotations

import re

from repobench.logging import get_logger
from repobench.models import InstructionProvenance, PullRequest

log = get_logger("tasks.instruction")


def extract_instruction(pr: PullRequest) -> tuple[str, InstructionProvenance, float]:
    """Extract instruction text and provenance from a PR.

    Priority hierarchy:
    1. Linked issue created before implementation (Tier A)
    2. PR problem statement / body (Tier B)
    3. PR title (Tier C - potentially solution-contaminated)

    Returns (instruction_text, provenance, confidence).
    """
    # --- Tier A: Linked issue ---
    if pr.linked_issue_number and pr.linked_issue_body:
        # Check if issue was created before merge
        if pr.linked_issue_created_at and pr.merged_at:
            if pr.linked_issue_created_at < pr.merged_at:
                text = _clean_instruction(pr.linked_issue_body, pr.title)
                if text:
                    log.debug(
                        "PR #%d: Tier A instruction from issue #%d",
                        pr.pr_number,
                        pr.linked_issue_number,
                    )
                    return text, InstructionProvenance.TIER_A, 0.95

        # Issue exists but timing unknown - still Tier A with lower confidence
        text = _clean_instruction(pr.linked_issue_body, pr.title)
        if text:
            log.debug(
                "PR #%d: Tier A instruction from issue #%d (timing unknown)",
                pr.pr_number,
                pr.linked_issue_number,
            )
            return text, InstructionProvenance.TIER_A, 0.85

    # --- Tier B: PR body problem statement ---
    if pr.body:
        text = _extract_problem_statement(pr.body)
        if text:
            log.debug("PR #%d: Tier B instruction from PR body", pr.pr_number)
            return text, InstructionProvenance.TIER_B, 0.75

    # --- Tier C: PR title ---
    if pr.title:
        text = _clean_title_as_instruction(pr.title)
        if text:
            log.debug("PR #%d: Tier C instruction from PR title", pr.pr_number)
            return text, InstructionProvenance.TIER_C, 0.50

    return "", InstructionProvenance.TIER_C, 0.0


def _clean_instruction(body: str, title: str) -> str:
    """Clean and extract a meaningful instruction from issue/PR body."""
    text = body.strip()

    # Remove common noise patterns
    noise_patterns = [
        r"---\s*\n.*?\n---\s*\n",  # Markdown front matter
        r"## (?:Screenshot|Screenshots|Demo|Video).*?(?=##|\Z)",  # Media sections
        r"## Checklist.*?(?=##|\Z)",  # Checklists
        r"## Environment.*?(?=##|\Z)",  # Environment info
        r"<!--.*?-->",  # HTML comments
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.DOTALL | re.IGNORECASE)

    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # If too short or empty after cleaning, fall back to title
    if len(text) < 20:
        return title.strip()

    # Truncate very long descriptions (keep first meaningful paragraph)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if paragraphs:
        # Return the first 2-3 paragraphs (the problem statement)
        result = "\n\n".join(paragraphs[:3])
        if len(result) > 2000:
            result = result[:2000] + "..."
        return result

    return text[:2000]


def _extract_problem_statement(body: str) -> str | None:
    """Try to extract a problem statement from PR body.

    Looks for structured sections like "Problem", "Context", "What",
    or the first meaningful paragraph.
    """
    sections = ["Problem", "Context", "What", "Description", "Summary", "Motivation"]
    for section in sections:
        pattern = rf"##?\s*{section}\s*\n(.*?)(?=\n##?\s|\Z)"
        match = re.search(pattern, body, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
            if len(text) >= 20:
                return text[:2000]

    # No structured section found - try first non-empty paragraph
    lines = body.strip().splitlines()
    paragraph_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph_lines:
                break
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("---"):
            continue
        paragraph_lines.append(stripped)

    text = " ".join(paragraph_lines)
    if len(text) >= 20:
        return text[:2000]

    return None


def _clean_title_as_instruction(title: str) -> str:
    """Convert a PR title into an instruction.

    Removes conventional-commit prefixes and PR numbers.
    """
    text = title.strip()

    # Remove conventional commit prefixes
    prefixes = [
        r"^feat[\(:]?\s*",
        r"^fix[\(:]?\s*",
        r"^chore[\(:]?\s*",
        r"^refactor[\(:]?\s*",
        r"^docs[\(:]?\s*",
        r"^test[\(:]?\s*",
        r"^ci[\(:]?\s*",
        r"^perf[\(:]?\s*",
        r"^style[\(:]?\s*",
        r"^build[\(:]?\s*",
        r"^revert[\(:]?\s*",
    ]
    for prefix in prefixes:
        text = re.sub(prefix, "", text, flags=re.IGNORECASE)

    # Remove trailing PR numbers like (#123)
    text = re.sub(r"\s*\(#\d+\)\s*$", "", text)

    # Remove scope parentheses
    text = re.sub(r"^\w+\(([^)]+)\):\s*", r"\1: ", text)

    # Capitalize first letter
    if text:
        text = text[0].upper() + text[1:]

    return text.strip()
