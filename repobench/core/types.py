"""Shared domain types for RepoBench.

These models are the contract between modules (repository/mining, tasks/validation,
execution, benchmark, analysis, reporting, cli). Keep this file stable — many modules
code against it and must not edit it.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repobench.core.errors import RepoBenchError

# Instruction provenance tiers (PRD §71-72):
#   A — pre-existing issue; B — strong PR problem statement;
#   C — possibly solution-contaminated (title / PR body with fix details);
#   D — LLM-derived from the implementation diff (opt-in generation,
#       repobench/tasks/generation.py) — derived from the solution by
#       construction, therefore ranked below C.
InstructionConfidence = Literal["A", "B", "C", "D"]

# On-disk task package layout (PRD §36). Single source of truth: TaskPackage.load
# is the only layout checker and repobench.tasks.package re-uses this constant.
PACKAGE_FILES = ("base.tar", "instruction.md", "gold.patch", "verifier.patch", "metadata.json")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskType(str, enum.Enum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    INTEGRATION = "integration"
    MIGRATION = "migration"
    PERFORMANCE = "performance"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class Complexity(str, enum.Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TaskStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    FILTERED = "FILTERED"
    PREPARING = "PREPARING"
    VALIDATING = "VALIDATING"
    VALID = "VALID"
    REJECTED = "REJECTED"


class RejectionCode(str, enum.Enum):
    NO_TEST_CHANGE = "NO_TEST_CHANGE"
    NO_INSTRUCTION = "NO_INSTRUCTION"
    TASK_TOO_SMALL = "TASK_TOO_SMALL"
    TASK_TOO_LARGE = "TASK_TOO_LARGE"
    HISTORY_UNSUPPORTED = "HISTORY_UNSUPPORTED"
    BASELINE_BROKEN = "BASELINE_BROKEN"
    ENVIRONMENT_UNSUPPORTED = "ENVIRONMENT_UNSUPPORTED"
    NOOP_PASSES = "NOOP_PASSES"
    GOLD_FAILS = "GOLD_FAILS"
    GOLD_REGRESSION = "GOLD_REGRESSION"
    FLAKY_VERIFIER = "FLAKY_VERIFIER"
    UNSAFE_TEST_SPLIT = "UNSAFE_TEST_SPLIT"
    LEAKAGE_HIGH = "LEAKAGE_HIGH"


class TrialOutcome(str, enum.Enum):
    SOLVED = "SOLVED"
    UNSOLVED = "UNSOLVED"
    HARNESS_ERROR = "HARNESS_ERROR"
    TIMEOUT = "TIMEOUT"
    SETUP_ERROR = "SETUP_ERROR"
    VERIFIER_ERROR = "VERIFIER_ERROR"


class OutputMode(str, enum.Enum):
    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"


class UsageRecord(BaseModel):
    """Token/cost usage as reported by a harness. All fields optional — never invented (PRD §53-54)."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    requests: int | None = None
    tool_calls: int | None = None
    reported_cost_usd: float | None = None


class CommandSpec(BaseModel):
    """A local command to run (PRD §20). No shell=True for official adapters."""

    argv: list[str]
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)  # empty dict = inherit parent env
    stdin: str | None = None
    timeout_seconds: int = 1200
    output_mode: OutputMode = OutputMode.TEXT


class ProcessResult(BaseModel):
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    # Typed spawn-failure contract: exception text when the process could not
    # be started at all (missing binary, bad cwd, ...), else None. Consumers
    # branch on this instead of matching sentinel strings in stderr.
    spawn_error: str | None = None


class ExecutionTarget(BaseModel):
    """Harness + Model + provider/config + harness configuration (PRD §15-16)."""

    id: str = ""
    harness: str
    model: str | None = None
    provider: str | None = None
    args: list[str] = Field(default_factory=list)
    timeout_minutes: int | None = None
    # Generic command adapter (harness == "command", PRD §25-26)
    command: list[str] | None = None
    output: OutputMode = OutputMode.TEXT
    env: dict[str, str] = Field(default_factory=dict)


class IssueInfo(BaseModel):
    number: int
    title: str = ""
    body: str = ""
    created_at: datetime | None = None


class PRInfo(BaseModel):
    number: int
    title: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str | None = None
    is_bot: bool = False
    base_sha: str | None = None
    head_sha: str | None = None
    merge_sha: str | None = None
    merged_at: datetime | None = None
    created_at: datetime | None = None
    linked_issue: IssueInfo | None = None
    changed_files: list[str] = Field(default_factory=list)


class Assessment(BaseModel):
    """Mining-derived assessment of one merged PR as a potential eval task.

    Single source of truth for the 11 mining fields: carried by CandidateInfo
    and embedded (not mirrored) in TaskMetadata, so readers/writers share one API.
    """

    task_type: TaskType = TaskType.UNKNOWN
    subsystem: str = "unknown"
    complexity: Complexity = Complexity.MEDIUM
    language: str | None = None
    instruction: str = ""
    instruction_confidence: InstructionConfidence = "C"
    instruction_source: str | None = None
    implementation_loc: int = 0
    test_loc: int = 0
    implementation_files: int = 0
    test_files: int = 0


class CandidateInfo(BaseModel):
    """A merged PR assessed as a potential retrospective eval task (PRD §65-72)."""

    candidate_id: str
    pr: PRInfo
    status: TaskStatus = TaskStatus.DISCOVERED
    rejection_code: RejectionCode | None = None
    assessment: Assessment = Field(default_factory=Assessment)


class TaskMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    pr_number: int | None = None
    title: str = ""
    base_sha: str
    gold_sha: str
    created_at: datetime | None = None
    status: TaskStatus = TaskStatus.DISCOVERED
    rejection_code: RejectionCode | None = None
    version: int = 1
    package_dir: str | None = None
    assessment: Assessment = Field(default_factory=Assessment)


class TaskPackage(BaseModel):
    """On-disk task package (PRD §36): base.tar, instruction.md, gold.patch, verifier.patch, metadata.json."""

    task_id: str
    directory: Path
    metadata: TaskMetadata

    @property
    def base_tar(self) -> Path:
        return self.directory / "base.tar"

    @property
    def instruction_md(self) -> Path:
        return self.directory / "instruction.md"

    @property
    def gold_patch(self) -> Path:
        return self.directory / "gold.patch"

    @property
    def verifier_patch(self) -> Path:
        return self.directory / "verifier.patch"

    @property
    def metadata_json(self) -> Path:
        return self.directory / "metadata.json"

    @classmethod
    def load(cls, directory: Path) -> TaskPackage:
        directory = Path(directory)
        missing = [name for name in PACKAGE_FILES if not (directory / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"task package at {directory} is missing: {', '.join(missing)}"
            )
        try:
            metadata = TaskMetadata.model_validate(
                json.loads((directory / "metadata.json").read_text())
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            # A package without parseable metadata is unusable — never silently
            # downgrade to a metadata-less package.
            raise RepoBenchError(
                f"metadata.json in task package {directory} is corrupt: {exc}"
            ) from exc
        return cls(
            task_id=metadata.task_id,
            directory=directory,
            metadata=metadata,
        )

    def instruction_text(self) -> str:
        return self.instruction_md.read_text()


class WorkloadDistribution(BaseModel):
    """Normalized shares (0-1) of the Workload Universe per dimension (PRD §66)."""

    task_type: dict[str, float] = Field(default_factory=dict)
    subsystem: dict[str, float] = Field(default_factory=dict)
    complexity: dict[str, float] = Field(default_factory=dict)


class AnalyzeSummary(BaseModel):
    total_merged_prs: int = 0
    task_candidates: int = 0
    validated_candidates: int = 0
    workload: WorkloadDistribution = WorkloadDistribution()
    suggested_benchmark_size: int = 0


class TrialResult(BaseModel):
    """Result of one Task × ExecutionTarget execution (PRD §100)."""

    trial_id: str
    run_id: str | None = None
    benchmark_id: str | None = None
    task_id: str
    target_id: str
    harness: str | None = None
    harness_version: str | None = None
    model: str | None = None
    provider: str | None = None
    # Required since 0.4.0: every construction site names its outcome — the old
    # HARNESS_ERROR default silently mislabeled results built without one.
    outcome: TrialOutcome
    # Harness output artifacts (PRD §121): capped stdout/stderr written next to
    # trial.json; paths recorded so audit tooling can find them.
    stdout_path: str | None = None
    stderr_path: str | None = None
    started_at: datetime | None = None
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    usage: UsageRecord | None = None
    task_verified: bool | None = None
    regression_verified: bool | None = None
    cost_usd: float | None = None
    cost_source: Literal["HARNESS_REPORTED", "USER_PROVIDED_PRICING"] | None = None
    files_changed: int | None = None
    loc_added: int | None = None
    loc_removed: int | None = None
    agent_patch: str | None = None
    prompt_path: str | None = None
    workspace: str | None = None
    error: str | None = None
