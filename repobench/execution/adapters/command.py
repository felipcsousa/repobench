"""Generic Command adapter (PRD §25-26).

Tier-2 escape hatch: the user configures an explicit argv list with placeholders.
No shell interpolation ever happens — placeholders are substituted with plain
string replacement on each argv element. Because the command is user-provided
and per-target, the binary cannot be probed generically: detect() reports the
adapter as available and notes that per-target commands cannot be probed.

Placeholders: {workspace} {prompt} {prompt_file} {task_id} {target_id}

{prompt_file} points at a file this adapter writes NEXT TO the workspace
(<trial>/prompt.md), never inside the repository, so it can never pollute the
captured agent patch (PRD §60).
"""

from __future__ import annotations

import re
from pathlib import Path

from repobench.core.types import CommandSpec, ExecutionTarget
from repobench.execution.adapters.base import (
    HarnessAdapter,
    HarnessCapabilities,
    HarnessDetection,
    HarnessResult,
    ValidationResult,
)

ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset(
    {"workspace", "prompt", "prompt_file", "task_id", "target_id"}
)

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")

PROMPT_FILE_NAME = "prompt.md"


def find_placeholders(command: list[str]) -> set[str]:
    """All placeholder names appearing in the command template."""
    found: set[str] = set()
    for element in command:
        found.update(_PLACEHOLDER_RE.findall(element))
    return found


class CommandAdapter(HarnessAdapter):
    name = "command"
    binary = ""
    # A generic command has no model flag concept: the user embeds any model
    # choice directly in the command template, so no capability is claimed.
    capabilities = HarnessCapabilities(model_override=False)

    def detect(self) -> HarnessDetection:
        # Generic commands can't be probed generically — availability is checked
        # per target at spawn time (a missing binary surfaces as SETUP_ERROR).
        return HarnessDetection(installed=True)

    def validate_target(self, target: ExecutionTarget) -> ValidationResult:
        errors: list[str] = []
        command = target.command
        if not command or not all(isinstance(part, str) and part for part in command):
            errors.append(
                "generic command targets require a non-empty 'command' list of strings "
                "(PRD §25)"
            )
        else:
            for name in sorted(find_placeholders(command)):
                if name not in ALLOWED_PLACEHOLDERS:
                    allowed = ", ".join(sorted(f"{{{p}}}" for p in ALLOWED_PLACEHOLDERS))
                    errors.append(
                        f"unknown placeholder {{{name}}} in command; allowed: {allowed}"
                    )
        return ValidationResult(valid=not errors, errors=errors)

    def build_command(
        self,
        target: ExecutionTarget,
        prompt: str,
        workspace: Path,
        *,
        task_id: str = "",
        target_id: str = "",
        timeout_seconds: int = 1200,
    ) -> CommandSpec:
        command = list(target.command or [])
        # The prompt file always lives NEXT TO the workspace (<trial>/prompt.md),
        # never inside the repository, so it can never pollute the captured agent
        # patch (PRD §60). The path is derived here — callers never pass one in.
        prompt_path = workspace.parent / PROMPT_FILE_NAME
        if any("{prompt_file}" in element for element in command):
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt)

        substitutions = {
            "{workspace}": str(workspace),
            "{prompt}": prompt,
            "{prompt_file}": str(prompt_path),
            "{task_id}": task_id,
            "{target_id}": target_id,
        }
        argv: list[str] = []
        for element in command:
            for placeholder, value in substitutions.items():
                element = element.replace(placeholder, value)
            argv.append(element)

        return CommandSpec(
            argv=argv,
            cwd=workspace,
            env={},
            timeout_seconds=timeout_seconds,
            output_mode=target.output,
        )

    def parse_output(self, stdout: str, stderr: str) -> HarnessResult:
        # Generic commands report no usage — never invent any (PRD §54).
        return super().parse_output(stdout, stderr)
