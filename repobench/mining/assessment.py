"""Candidate assessment: deterministic 0-1 component scores per PR (PRD §11-24).

Adaptive mining replaces the absolute size gates with scored evidence: every
component scorer is a pure, deterministic heuristic — no ML, no randomness
(PRD §72) — and returns a `ComponentScore` whose evidence list names exactly
the signals that fired, so every stored score is explainable (PRD §71) and
reproducible from the stored features. Honesty rule (PRD §53-54): evidence
never invents a signal; absence of data lowers a score or stays neutral, it is
never papered over.

Scores are rounded to 2 decimals inside each component so the stored value
matches recomputation from the stored features exactly; the aggregate rounds
once more at the boundary. `ASSESSMENT_VERSION` (PRD §69) marks the algorithm
that produced them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from repobench.config import ProjectConfig, TaskMiningConfig
from repobench.core.types import CandidateClass
from repobench.mining.instruction import InstructionResult
from repobench.mining.scope import ScopeFeatures

# Version of this scoring algorithm (PRD §69) — recorded on every scored
# assessment so historical benchmarks stay reproducible as heuristics evolve.
ASSESSMENT_VERSION = "adaptive-v1"

# Auditable aggregate weights (PRD §12). CandidateScore does not decide
# correctness — it ranks which candidates are worth validating first (PRD §13).
CANDIDATE_SCORE_WEIGHTS: dict[str, float] = {
    "instruction": 0.25,
    "verifier": 0.25,
    "reconstructability": 0.20,
    "isolation": 0.10,
    "environment": 0.10,
    "scope": 0.10,
}


class ComponentScore(BaseModel):
    """One scored component: a 0-1 score plus the evidence that produced it."""

    model_config = ConfigDict(frozen=True)

    score: float
    evidence: list[str]


# Instruction provenance tiers (PRD §14). Tier D is documented for completeness
# only — it is never produced at mining time: LLM-derived instructions are
# generated at benchmark build (repobench/tasks/generation.py), so a D here
# would be invented provenance.
_INSTRUCTION_TIER_SCORES: dict[str, float] = {"A": 0.95, "B": 0.75, "C": 0.45, "D": 0.30}

_TIER_EVIDENCE: dict[str, str] = {
    "A": "linked issue predates PR (tier A)",
    "B": "PR body problem statement (tier B)",
    "C": "PR body flagged solution-contaminated (tier C)",
    "D": "LLM-derived from the diff (tier D; never produced at mining time)",
}


def assess_instruction(instruction: InstructionResult | None) -> ComponentScore:
    """Instruction quality by provenance tier (PRD §14): A=0.95, B=0.75, C=0.45,
    D=0.30 (unreachable at mining time — see _INSTRUCTION_TIER_SCORES). A PR
    without any usable instruction scores 0.0; the caller hard-gates it as
    NO_INSTRUCTION anyway (PRD §9)."""
    if instruction is None:
        return ComponentScore(score=0.0, evidence=["no instruction"])
    confidence = instruction.confidence
    evidence = [_TIER_EVIDENCE.get(confidence, f"instruction tier {confidence}")]
    if confidence == "C" and instruction.source == "title":
        evidence = ["title-derived, possibly solution-contaminated (tier C)"]
    return ComponentScore(score=_INSTRUCTION_TIER_SCORES[confidence], evidence=evidence)


# Known test-runner substrings for the environment evidence label (PRD §23:
# "pnpm + vitest -> high"). Only a hint for the evidence line — the score
# depends on the command being known, never on which runner it names.
_RUNNER_HINTS = ("pytest", "vitest", "jest", "go test", "cargo test", "mocha")


def _runner_hint(test_command: str) -> str | None:
    lowered = test_command.lower()
    return next((runner for runner in _RUNNER_HINTS if runner in lowered), None)


def assess_verifier(features: ScopeFeatures) -> ComponentScore:
    """Verifier evidence precheck (PRD §15): cheap diff signals for how strong
    the eventual hidden verifier could be — base 0.40, plus credit for multiple
    test files, meaningful test LOC, substantial test LOC, and a healthy
    test-to-implementation ratio. NEVER a verdict: real no-op/oracle validation
    decides correctness later (PRD §81) — this only ranks candidates."""
    score = 0.40
    evidence: list[str] = []
    if features.test_files >= 2:
        score += 0.20
        evidence.append(f"{features.test_files} test files")
    if features.test_loc >= 40:
        score += 0.20
        evidence.append(f"{features.test_loc} test LOC")
    if features.test_loc >= 150:
        score += 0.20
        evidence.append("substantial test LOC (>= 150)")
    ratio = features.test_loc / max(features.implementation_loc, 1)
    if ratio >= 0.3:
        score += 0.10
        evidence.append(f"test/impl ratio {ratio:.2f}")
    return ComponentScore(score=round(min(score, 1.0), 2), evidence=evidence)


def assess_reconstructability(
    base_sha: str | None,
    head_sha: str | None,
    merge_sha: str | None,
    resolvable: bool,
) -> ComponentScore:
    """History reconstruction confidence (PRD §16). `resolvable` is the
    caller's verdict that all three SHAs are present and `rev_parse` verified
    base/head — clean history scores 1.00. A missing merge SHA leaves the merge
    style unproven (0.30); unresolvable SHAs score 0.0 (the caller hard-gates
    them as HISTORY_UNSUPPORTED anyway, PRD §9)."""
    if resolvable:
        return ComponentScore(score=1.0, evidence=["clean history: base/head/merge SHAs resolvable"])
    if not merge_sha:
        return ComponentScore(score=0.30, evidence=["merge SHA missing"])
    return ComponentScore(score=0.0, evidence=["history unresolvable: base/head SHAs do not verify"])


def assess_scope(scope_percentile: float | None) -> ComponentScore:
    """Scope confidence from the repository-relative percentile (PRD §17-18):
    a diff is large or small only against the repository's own distribution, so
    confidence is 1 - P/100. None (baseline too small to rank against) is
    neutral 0.50 — never an invented P0/P100."""
    if scope_percentile is None:
        return ComponentScore(score=0.50, evidence=["no repository baseline (n too small)"])
    confidence = round(max(0.0, 1.0 - scope_percentile / 100.0), 2)
    return ComponentScore(score=confidence, evidence=[f"P{scope_percentile:g} of the repository baseline"])


def assess_isolation(features: ScopeFeatures, subsystem: str) -> ComponentScore:
    """Isolation confidence (PRD §22): does the diff look like one coherent
    behavior change? Single package and few directories add confidence; many
    packages or languages subtract it. `subsystem` is context only — it names
    where the change lives in the evidence but never moves the score."""
    score = 0.60
    evidence: list[str] = []
    if features.packages_touched == 1:
        score += 0.20
        evidence.append("single package")
    if features.dirs_touched <= 2:
        score += 0.10
        evidence.append("coherent paths")
    if features.packages_touched >= 3:
        score -= 0.20
        evidence.append("multi-package change")
    if len(features.languages) >= 2:
        score -= 0.20
        evidence.append("cross-language implementation")
    if subsystem and subsystem != "unknown":
        evidence.append(f"subsystem {subsystem}")
    return ComponentScore(score=round(min(max(score, 0.0), 1.0), 2), evidence=evidence)


def assess_environment(project_cfg: ProjectConfig) -> ComponentScore:
    """Environment confidence from what is actually configured (PRD §23).

    Honesty rule: only known signals count — an empty ProjectConfig scores 0.0,
    it never borrows a default it did not detect. Callers without a detected
    project environment use `environment_unknown()` instead."""
    score = 0.0
    evidence: list[str] = []
    if project_cfg.language:
        score += 0.10
        evidence.append(f"language {project_cfg.language}")
    if project_cfg.package_manager:
        score += 0.20
        evidence.append(f"package manager {project_cfg.package_manager}")
    if project_cfg.install_command:
        score += 0.20
        evidence.append("install command known")
    if project_cfg.test_command:
        score += 0.30
        runner = _runner_hint(project_cfg.test_command)
        evidence.append(f"test command known ({runner})" if runner else "test command known")
    if project_cfg.regression_command:
        score += 0.20
        evidence.append("regression command known")
    return ComponentScore(score=round(min(score, 1.0), 2), evidence=evidence)


def environment_unknown() -> ComponentScore:
    """Neutral environment component when no ProjectConfig reached mining: a
    fixed midpoint with explicit evidence, never invented signals (PRD §23)."""
    return ComponentScore(score=0.50, evidence=["project environment unknown"])


def aggregate(components: dict[str, ComponentScore]) -> float:
    """Weighted CandidateScore (PRD §12), weights in CANDIDATE_SCORE_WEIGHTS.

    Requires all six keyed components; ranks validation priority only — it
    never decides correctness (PRD §12-13, §81)."""
    total = sum(weight * components[name].score for name, weight in CANDIDATE_SCORE_WEIGHTS.items())
    return round(total, 2)


def classify(score: float, cfg: TaskMiningConfig) -> CandidateClass:
    """Human-readable candidate class (PRD §24) from configurable thresholds:
    >= high_confidence -> HIGH_CONFIDENCE, >= medium_confidence ->
    MEDIUM_CONFIDENCE, else EXPERIMENTAL."""
    if score >= cfg.candidate_score.high_confidence:
        return CandidateClass.HIGH_CONFIDENCE
    if score >= cfg.candidate_score.medium_confidence:
        return CandidateClass.MEDIUM_CONFIDENCE
    return CandidateClass.EXPERIMENTAL


def risk_flags(
    features: ScopeFeatures,
    percentile: float | None,
    n_baseline: int,
    test_loc: int,
) -> list[str]:
    """Deterministic, sorted risk flags (PRD §11): warnings a reviewer sees
    beside the score, never rejections. `percentile` should be the value stored
    on the assessment so flags recompute exactly from stored fields."""
    flags: set[str] = set()
    if percentile is not None:
        if percentile >= 80:
            flags.add("large_diff")
        if percentile >= 95:
            flags.add("xlarge_diff")
    if features.generated_loc > 0 and features.generated_loc > (
        features.implementation_loc - features.generated_loc
    ):
        flags.add("generated_heavy")
    if features.packages_touched >= 3:
        flags.add("multi_package")
    if len(features.languages) >= 2:
        flags.add("cross_language")
    if test_loc < 10:
        flags.add("weak_verifier_evidence")
    if n_baseline < 20:
        flags.add("small_repository_baseline")
    return sorted(flags)
