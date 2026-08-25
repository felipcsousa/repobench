"""Core data models for RepoBench."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── Enums ──────────────────────────────────────────────────────────────────────


class TaskType(str, Enum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    UNKNOWN = "unknown"


class Complexity(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TaskStatus(str, Enum):
    DISCOVERED = "discovered"
    FILTERED = "filtered"
    BUILDING = "building"
    VALIDATING = "validating"
    VALID = "valid"
    REJECTED = "rejected"


class RejectionReason(str, Enum):
    NO_TEST_CHANGE = "NO_TEST_CHANGE"
    NO_INSTRUCTION = "NO_INSTRUCTION"
    HISTORY_UNSUPPORTED = "HISTORY_UNSUPPORTED"
    ENVIRONMENT_UNSUPPORTED = "ENVIRONMENT_UNSUPPORTED"
    BASELINE_BROKEN = "BASELINE_BROKEN"
    NOOP_PASSES = "NOOP_PASSES"
    GOLD_FAILS = "GOLD_FAILS"
    FLAKY_VERIFIER = "FLAKY_VERIFIER"
    LEAKAGE_HIGH = "LEAKAGE_HIGH"
    TASK_TOO_LARGE = "TASK_TOO_LARGE"
    TASK_TOO_SMALL = "TASK_TOO_SMALL"
    UNSUPPORTED_HISTORY_STRATEGY = "UNSUPPORTED_HISTORY_STRATEGY"
    NO_VERIFIER_EVIDENCE = "NO_VERIFIER_EVIDENCE"


class InstructionProvenance(str, Enum):
    TIER_A = "A"  # Linked issue created before implementation
    TIER_B = "B"  # PR contains verifiable problem statement
    TIER_C = "C"  # PR title/body potentially solution-contaminated


class NetworkIsolation(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


# ── Configuration ──────────────────────────────────────────────────────────────


class TaskMiningConfig(BaseModel):
    min_implementation_loc: int = 20
    max_implementation_loc: int = 400
    max_implementation_files: int = 8
    supported_types: list[str] = Field(default_factory=lambda: ["bugfix", "feature"])
    require_test_change: bool = True


class BenchmarkDimensions(BaseModel):
    task_type: float = 0.30
    subsystem: float = 0.40
    complexity: float = 0.30


class BenchmarkConfig(BaseModel):
    size: int = 24
    dimensions: BenchmarkDimensions = Field(default_factory=BenchmarkDimensions)


class ExecutionConfig(BaseModel):
    environment: str = "docker"  # docker | local
    concurrency: int = 4
    harbor_timeout: int = 600  # seconds per trial
    network_mode: str = "no-network"  # no-network | public | allowlist


class VerificationConfig(BaseModel):
    test_globs: list[str] = Field(
        default_factory=lambda: [
            # Python
            "test_*.py",
            "*_test.py",
            "tests/**",
            # JavaScript/TypeScript
            "*.test.ts",
            "*.test.tsx",
            "*.spec.ts",
            "*.spec.tsx",
            "__tests__/**",
            # Go
            "*_test.go",
            "**/testdata/**",
            # Java
            "*Test.java",
            "*Tests.java",
            "*IT.java",
            "**/src/test/**",
        ]
    )
    verifier_asset_globs: list[str] = Field(
        default_factory=lambda: ["test/fixtures/**", "**/__snapshots__/**"]
    )


class LeakageConfig(BaseModel):
    mode: str = "best_available"


class RepositoryConfig(BaseModel):
    provider: str = "github"
    lookback_days: int = 180


class ProjectConfig(BaseModel):
    languages: list[str] = Field(default_factory=list)
    install_command: str | None = None
    build_command: str | None = None
    test_command: str | None = None


class AgentConfig(BaseModel):
    agent: str
    model: str | None = None
    reasoning: str | None = None


class RepoBenchConfig(BaseModel):
    version: int = 1
    repository: RepositoryConfig = Field(default_factory=RepositoryConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    task_mining: TaskMiningConfig = Field(default_factory=TaskMiningConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    leakage: LeakageConfig = Field(default_factory=LeakageConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)


# ── Repository / PR ────────────────────────────────────────────────────────────


class PullRequest(BaseModel):
    pr_number: int
    title: str
    body: str | None = None
    author: str
    author_type: str | None = None  # "user", "bot", "app"
    labels: list[str] = Field(default_factory=list)
    merged_at: datetime | None = None
    merge_sha: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    linked_issue_number: int | None = None
    linked_issue_body: str | None = None
    linked_issue_created_at: datetime | None = None
    merge_commit_sha: str | None = None
    head_commit_sha: str | None = None
    diff_url: str | None = None


class PRWorkloadInfo(BaseModel):
    """Enriched PR info for the Workload Universe."""

    pr: PullRequest
    task_type: TaskType
    task_type_confidence: float
    subsystem: str
    complexity: Complexity
    implementation_loc: int
    implementation_files: int
    test_loc: int
    test_files: int
    languages: list[str] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)


# ── Candidate / Task ──────────────────────────────────────────────────────────


class Eligibility(BaseModel):
    history: bool = False
    instruction: bool = False
    verifier: bool = False
    environment: bool | None = None
    oracle: bool | None = None
    determinism: bool | None = None
    leakage: bool | None = None


class CandidateTask(BaseModel):
    candidate_id: str = Field(default_factory=lambda: _gen_id("af_c"))
    pr_number: int
    pr_title: str = ""
    base_sha: str = ""
    gold_sha: str = ""
    merge_commit_sha: str | None = None
    head_commit_sha: str | None = None

    task_type: TaskType = TaskType.UNKNOWN
    task_type_confidence: float = 0.0
    subsystem: str = "unknown"
    complexity: Complexity = Complexity.MEDIUM

    implementation_loc: int = 0
    implementation_files: int = 0
    test_loc: int = 0
    test_files: int = 0

    instruction_source: str = ""
    instruction_provenance: InstructionProvenance | None = None
    instruction_text: str | None = None

    status: TaskStatus = TaskStatus.DISCOVERED
    rejection_reason: RejectionReason | None = None

    eligibility: Eligibility = Field(default_factory=Eligibility)

    # Leakage
    leakage_risk: float = 0.0
    leakage_warnings: list[str] = Field(default_factory=list)
    network_isolation: NetworkIsolation = NetworkIsolation.NONE

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ValidTask(BaseModel):
    """A validated task ready for benchmark sampling."""

    task_id: str = Field(default_factory=lambda: _gen_id("af_t"))
    candidate: CandidateTask
    instruction_text: str
    verifier_files: list[str] = Field(default_factory=list)
    implementation_files_list: list[str] = Field(default_factory=list)


# ── Benchmark ──────────────────────────────────────────────────────────────────


class BenchmarkHealth(BaseModel):
    overall: int = 0
    representativeness: int = 0
    validation: int = 0
    leakage: int = 0
    recency: int = 0
    diversity: int = 0


class BenchmarkManifest(BaseModel):
    benchmark_id: str = Field(default_factory=lambda: _gen_id("af_b"))
    repository_remote: str = ""
    repository_private: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    workload_window_days: int = 180
    workload_window_prs: int = 0
    tasks: list[str] = Field(default_factory=list)
    health: BenchmarkHealth = Field(default_factory=BenchmarkHealth)
    coverage_warnings: list[str] = Field(default_factory=list)


# ── Execution ──────────────────────────────────────────────────────────────────


class AgentConfiguration(BaseModel):
    name: str
    agent: str
    model: str | None = None
    reasoning: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class VerifierResult(BaseModel):
    task: bool | None = None
    regression: bool | None = None


class Trial(BaseModel):
    trial_id: str = Field(default_factory=lambda: _gen_id("af_tr"))
    benchmark_id: str = ""
    task_id: str = ""
    agent_config: str = ""

    solved: bool = False
    duration_ms: int | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None

    verifier: VerifierResult = Field(default_factory=VerifierResult)

    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunResult(BaseModel):
    run_id: str = Field(default_factory=lambda: _gen_id("af_run"))
    benchmark_id: str = ""
    agent_config: str = ""
    agent_version: str = ""
    model_name: str = ""
    harbor_version: str = ""
    trials: list[Trial] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Report ─────────────────────────────────────────────────────────────────────


class ConfigMetrics(BaseModel):
    solved: int = 0
    total: int = 0
    pass_rate: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    total_cost: float = 0.0
    cost_per_solve: float | None = None
    mean_cost_task: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    tokens_per_solve: int | None = None
    p50_duration_ms: int | None = None
    p90_duration_ms: int | None = None


class ComparisonPair(BaseModel):
    config_a: str
    config_b: str
    difference_pp: float
    ci_lower_pp: float
    ci_upper_pp: float
    conclusive: bool


class SegmentResult(BaseModel):
    category: str  # e.g. "bugfix", "payments"
    metrics: dict[str, ConfigMetrics] = Field(default_factory=dict)


class Report(BaseModel):
    benchmark_id: str
    repository: str
    tasks: int
    health: BenchmarkHealth
    config_metrics: dict[str, ConfigMetrics] = Field(default_factory=dict)
    comparisons: list[ComparisonPair] = Field(default_factory=list)
    segments: list[SegmentResult] = Field(default_factory=list)
    recommendation: str | None = None
    recommendation_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    public_repo_warning: bool = False
