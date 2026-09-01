"""Tier-D instruction generation (PRD §71-72, opt-in extension).

For candidates without strong instruction provenance (no `gh` issue / PR body —
the offline case that falls back to the commit title, confidence C) an LLM may
draft a directional task instruction by reading the gold IMPLEMENTATION diff.

Methodology rules enforced here:

- Generation happens ONLY in `benchmark build`, never in `analyze`
  (analyze must stay token-free, PRD §10) — and only when explicitly enabled.
- The generation prompt receives the PR title, the changed file list and the
  gold (implementation) patch. The verifier/test patch is the hidden answer
  key and is NEVER part of the prompt — by construction, this module only ever
  reads `package.gold_patch`.
- A deterministic anti-solution validator rejects drafts that quote added
  patch lines or mention code-like identifiers from the patch. On validator
  failure after one retry the build falls back to the title-derived
  instruction (confidence C behavior) and records the failure.

Because a D instruction is derived from the solution by construction, it ranks
BELOW C in `InstructionConfidence`; the prompt/validator mitigate but do not
eliminate that contamination channel. Report the tier mix whenever D is used.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pydantic

from repobench.config import InstructionGenerationConfig
from repobench.core.ids import sha256_hex
from repobench.core.types import CandidateInfo, ExecutionTarget, TaskPackage
from repobench.execution.adapters.registry import get_adapter
from repobench.execution.process import run_sync

_MAX_ATTEMPTS = 2  # initial attempt + 1 retry on validator failure
_MIN_INSTRUCTION_CHARS = 80
_MAX_INSTRUCTION_CHARS = 4000
_MIN_QUOTED_LINE_CHARS = 20

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_HAS_INNER_CASE_RE = re.compile(r"[a-z][A-Z]")

# Generic English words that would otherwise look code-like and make the
# validator reject every well-behaved instruction.
_STOPWORDS = frozenset(
    {
        "function",
        "return",
        "change",
        "update",
        "file",
        "test",
        "value",
        "error",
        "code",
        "should",
        "must",
        "when",
        "this",
        "that",
    }
)


class GenerationOutcome(pydantic.BaseModel):
    """Result of one instruction-generation attempt series for a candidate."""

    text: str | None  # generated instruction, None on failure
    violations: list[str]  # validator findings from the last attempt
    failed_reason: str | None  # spawn/parse/timeout failure description
    attempts: int
    metadata: dict  # {target, harness, model, harness_version, generation_prompt_sha256, attempts}


def _added_lines(patch: str) -> list[str]:
    """Stripped content of the patch's added lines ('+' lines, headers excluded)."""
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("diff --git "):
            continue
        if line.startswith("+"):
            content = line[1:].strip()
            if content:
                lines.append(content)
    return lines


def _code_like_identifiers(patch: str) -> set[str]:
    """Code-like identifier tokens appearing in the patch's added lines.

    Code-like: at least 4 characters AND (contains '_' or inner camelCase).
    Generic stopwords are excluded so plain-English instructions stay valid.
    """
    identifiers: set[str] = set()
    for line in _added_lines(patch):
        for token in _IDENT_RE.findall(line):
            if token.lower() in _STOPWORDS:
                continue
            if "_" in token or _HAS_INNER_CASE_RE.search(token):
                identifiers.add(token)
    return identifiers


def _strip_path_operand(operand: str) -> str:
    """Clean one diff path operand: drop quotes, timestamps and the a/ b/ prefix."""
    operand = operand.strip()
    if operand.startswith('"') and operand.endswith('"') and len(operand) >= 2:
        operand = operand[1:-1]
    operand = operand.split("\t", 1)[0].strip()
    if len(operand) > 2 and operand[1] == "/" and operand[0] in "ab":
        return operand[2:]
    return operand


def _patch_files(patch: str) -> list[str]:
    """Changed file paths of a unified diff, in order (b-side preferred)."""
    files: list[str] = []
    a_path: str | None = None  # staged a-side, flushed when the b-side arrives
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            if a_path is not None:
                files.append(a_path)
            a_path = None
        elif line.startswith("--- "):
            operand = _strip_path_operand(line[4:])
            a_path = None if operand == "/dev/null" else operand
        elif line.startswith("+++ "):
            operand = _strip_path_operand(line[4:])
            if operand == "/dev/null":
                if a_path is not None:
                    files.append(a_path)
            else:
                files.append(operand)
            a_path = None
    if a_path is not None:
        files.append(a_path)
    return files


def build_generation_prompt(
    title: str, implementation_patch: str, changed_files: list[str]
) -> str:
    """Rules-based prompt asking for a problem description of the change.

    Inputs are the PR title, the gold IMPLEMENTATION patch and the changed file
    list — never the verifier/test patch (that is the hidden answer key).
    """
    files = "\n".join(f"- {name}" for name in changed_files) or "- (file list unavailable)"
    return f"""You are drafting the task instruction for a software engineering benchmark task.

You are given the pull request title and the implementation diff of an already merged change. Write a short task instruction (about 10 lines) that describes the PROBLEM the change addresses: the symptoms, the requirement, and the expected behavior — what should hold true once the work is done.

Hard rules:
- Describe the goal and the observable behavior, never the diff itself.
- Do NOT name any function, variable, identifier, or file introduced or changed by the diff.
- Do NOT quote, copy, or paraphrase any code from the diff.
- Do NOT use "change X to Y" / "replace X with Y" phrasing.
- Write in English, plain prose, no code blocks.

PR title:
{title.strip() or "(untitled)"}

Changed files:
{files}

Implementation diff (background context only — do not echo it back):
{implementation_patch}"""


def validate_generated_instruction(text: str, implementation_patch: str) -> list[str]:
    """Deterministic anti-solution validator; an empty list means valid.

    Rules: no non-trivial added patch line may be quoted, no code-like
    identifier from the added lines may be mentioned, and the text length must
    stay within the configured bounds.
    """
    violations: list[str] = []
    if not (_MIN_INSTRUCTION_CHARS <= len(text) <= _MAX_INSTRUCTION_CHARS):
        violations.append(
            f"instruction length {len(text)} is outside the "
            f"{_MIN_INSTRUCTION_CHARS}-{_MAX_INSTRUCTION_CHARS} character bounds"
        )

    lowered = text.lower()
    for line in _added_lines(implementation_patch):
        if len(line) >= _MIN_QUOTED_LINE_CHARS and line.lower() in lowered:
            violations.append(
                f"instruction quotes an added patch line: {line[:60]!r}"
            )
            break

    for identifier in sorted(_code_like_identifiers(implementation_patch)):
        if identifier.lower() in lowered:
            violations.append(
                f"instruction mentions the solution identifier {identifier!r}"
            )
            break
    return violations


def _extract_text(stdout: str) -> str:
    """Structured output first ('result'/'message'/'response'), else raw stdout."""
    stripped = stdout.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        for key in ("result", "message", "response"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return stripped


def generate_instruction(
    candidate: CandidateInfo,
    package: TaskPackage,
    target: ExecutionTarget,
    *,
    cfg: InstructionGenerationConfig,
    harness_version: str | None = None,
) -> GenerationOutcome:
    """Draft a task instruction with the target's harness (up to 2 attempts).

    Runs through the target's adapter (PRD §17: inference always goes through
    the user's installed harness CLIs — RepoBench never calls a model API) in a
    throwaway workspace with the inherited environment. This is not a trial: no
    TrialEnvironment, no sanitization, plain `run_sync`.
    """
    # The verifier patch is never read here: the prompt is built from the gold
    # implementation patch only.
    implementation_patch = package.gold_patch.read_text()
    prompt = build_generation_prompt(
        title=candidate.pr.title,
        implementation_patch=implementation_patch,
        changed_files=_patch_files(implementation_patch),
    )
    adapter = get_adapter(target.harness)
    metadata: dict = {
        "target": target.id,
        "harness": target.harness,
        "model": target.model,
        "harness_version": harness_version,
        "generation_prompt_sha256": sha256_hex(prompt),
    }

    violations: list[str] = []
    failed_reason: str | None = None
    attempts_used = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        attempts_used = attempt
        with tempfile.TemporaryDirectory(prefix="repobench-generation-") as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            spec = adapter.build_command(
                target,
                prompt,
                workspace,
                task_id=package.task_id,
                target_id=target.id,
                timeout_seconds=cfg.timeout_minutes * 60,
            )
            result = run_sync(
                spec.argv, spec.cwd, env=None, timeout_seconds=spec.timeout_seconds
            )

        if result.spawn_error:
            failed_reason = f"spawn failed: {result.spawn_error}"
            break
        if result.timed_out:
            failed_reason = f"timed out after {spec.timeout_seconds}s"
            break
        if result.exit_code != 0:
            stderr = result.stderr.strip()[:200]
            failed_reason = f"exit code {result.exit_code}" + (f": {stderr}" if stderr else "")
            break

        text = _extract_text(result.stdout)
        if not text:
            failed_reason = "empty output"
            break

        violations = validate_generated_instruction(text, implementation_patch)
        if not violations:
            return GenerationOutcome(
                text=text,
                violations=[],
                failed_reason=None,
                attempts=attempt,
                metadata={**metadata, "attempts": attempt},
            )

    return GenerationOutcome(
        text=None,
        violations=violations,
        failed_reason=failed_reason,
        attempts=attempts_used,
        metadata={**metadata, "attempts": attempts_used},
    )
