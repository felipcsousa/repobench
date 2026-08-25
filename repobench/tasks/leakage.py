"""Instruction leakage scanner.

Scans instructions for potential solution leakage by detecting identifiers
that exist in the gold patch but not in the base state.
"""

from __future__ import annotations

import re

from repobench.logging import get_logger

log = get_logger("tasks.leakage")


def scan_instruction_leakage(
    instruction_text: str,
    gold_files: dict[str, str] | None = None,
    base_files: dict[str, str] | None = None,
) -> tuple[float, list[str]]:
    """Scan instruction for potential solution leakage.

    Args:
        instruction_text: The task instruction text.
        gold_files: Map of filepath -> content after the change (gold state).
        base_files: Map of filepath -> content before the change (base state).

    Returns:
        (leakage_risk, warnings) where risk is 0.0-1.0.
    """
    if not instruction_text:
        return 0.0, []

    warnings: list[str] = []
    risk_score = 0.0

    # --- Extract identifiers from instruction ---
    instruction_identifiers = _extract_identifiers(instruction_text)

    if not instruction_identifiers:
        return 0.0, []

    # --- If we have gold/base files, do cross-reference scan ---
    if gold_files and base_files:
        gold_identifiers = _extract_all_identifiers(gold_files)
        base_identifiers = _extract_all_identifiers(base_files)

        # New identifiers: present in gold but not in base
        new_identifiers = gold_identifiers - base_identifiers

        # Check which instruction identifiers are "new"
        leaked = instruction_identifiers & new_identifiers
        if leaked:
            risk_score += 0.3 * min(len(leaked) / max(len(instruction_identifiers), 1), 1.0)
            for ident in sorted(leaked)[:10]:
                warnings.append(
                    f"POTENTIAL_SOLUTION_LEAKAGE: '{ident}' exists in gold but not in base"
                )

    # --- Check for solution-contaminated patterns in instruction ---
    contamination_warnings, contamination_risk = _check_contamination_patterns(instruction_text)
    warnings.extend(contamination_warnings)
    risk_score += contamination_risk

    # Cap risk at 1.0
    risk_score = min(risk_score, 1.0)

    return risk_score, warnings


def _extract_identifiers(text: str) -> set[str]:
    """Extract potential identifiers (class names, function names, etc.) from text."""
    identifiers: set[str] = set()

    # PascalCase class names
    pascal_pattern = r"\b([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)+)\b"
    for match in re.finditer(pascal_pattern, text):
        identifiers.add(match.group(1))

    # camelCase function/method names (after common verbs)
    camel_pattern = r"\b([a-z]+[A-Z][a-zA-Z0-9]+)\b"
    for match in re.finditer(camel_pattern, text):
        identifiers.add(match.group(1))

    # SCREAMING_SNAKE_CASE constants
    const_pattern = r"\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)\b"
    for match in re.finditer(const_pattern, text):
        identifiers.add(match.group(1))

    # snake_case that look like function definitions
    snake_func_pattern = r"\b([a-z][a-z0-9]+_[a-z][a-z0-9_]+)\b"
    for match in re.finditer(snake_func_pattern, text):
        word = match.group(1)
        # Filter out common English words
        common_words = {
            "the",
            "this",
            "that",
            "with",
            "from",
            "into",
            "should",
            "when",
            "where",
            "what",
            "which",
            "does",
            "have",
            "been",
            "will",
            "would",
            "could",
            "should",
            "also",
            "just",
        }
        if word.lower() not in common_words:
            identifiers.add(word)

    return identifiers


def _extract_all_identifiers(files: dict[str, str]) -> set[str]:
    """Extract all identifiers from a set of source files."""
    all_identifiers: set[str] = set()
    for content in files.values():
        all_identifiers |= _extract_identifiers(content)
    return all_identifiers


def _check_contamination_patterns(text: str) -> tuple[list[str], float]:
    """Check for patterns that suggest the instruction was generated from the solution."""
    warnings: list[str] = []
    risk = 0.0

    text_lower = text.lower()

    # Pattern: instruction mentions "the implementation" or "the fix" too specifically
    impl_ref_patterns = [
        (r"the (?:new|specific) (?:class|function|method|variable) `(\w+)`", 0.2),
        (r"(?:implement|create|add) `(\w+)` (?:class|function|method)", 0.2),
        (r"(?:change|modify|update) `(\w+)` to", 0.15),
        (r"(?:rename|refactor) `(\w+)` to `(\w+)`", 0.25),
    ]

    for pattern, weight in impl_ref_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            risk += weight
            warnings.append(
                f"SOLUTION_PATTERN: instruction references specific implementation detail: '{match.group(0)[:80]}'"
            )

    # Pattern: instruction contains exact file paths from the solution
    path_pattern = r"(?:src|lib|app|packages)/[\w/]+\.\w+"
    paths_found = re.findall(path_pattern, text)
    if paths_found:
        risk += 0.1 * min(len(paths_found) / 3, 1.0)
        for p in paths_found[:3]:
            warnings.append(f"PATH_LEAKAGE: instruction mentions specific path: '{p}'")

    return warnings, min(risk, 0.5)
