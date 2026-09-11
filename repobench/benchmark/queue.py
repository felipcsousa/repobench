"""The validation queue abstraction (PRD §25-29, §61-66).

`build_benchmark` owns the validation loop (reconstruction, leakage, the
validator); everything that DIFFERS between the adaptive mining strategy and
the legacy hard_caps mode lives here as two `ValidationStrategy`
implementations, so the loop carries no mode branching:

- `AdaptiveStrategy` (task_mining.size_policy == "adaptive"): class-filtered
  pool (PRD §33), coverage-aware re-picking after every validation
  (PRD §28-29), stop at requested size + min_representativeness (PRD §26,
  §61), and the coverage-need queue announcements (PRD §60).
- `HardCapsStrategy` (hard_caps / shadow): today's legacy semantics — fixed
  pool order, `--stop-when-sufficient` as the opt-in early stop, no class
  filter, no minimum-size gate.

`UniverseProfile` precomputes the Workload Universe share distributions once
per build (PRD §66 dimension order): the queue consults them on every
re-pick, so rebuilding them per candidate would repeat invariant work.

`TerminationReason` models how the loop ended explicitly; the build outcome's
insufficiency flag, warnings and budget marker are derived from that one fact.

Everything here is pure and deterministic (PRD §72): no randomness, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from repobench.benchmark.priority import (
    class_rank,
    coverage_need,
    representativeness,
    top_deficit,
    validation_priority,
)
from repobench.benchmark.sampling import (
    DIMENSION_NAMES,
    dimension_values,
    distribution_of,
)
from repobench.config import BenchmarkDimensions, RepoBenchConfig
from repobench.core.errors import UsageError
from repobench.core.types import Assessment, CandidateClass, CandidateInfo


# ------------------------------------------------------------ universe profile


@dataclass(frozen=True)
class UniverseProfile:
    """Precomputed universe share-distributions (PRD §66 dimension order) —
    invariant for the whole build, computed once (the review's O(N^3) fix:
    the adaptive queue re-picks after every validation, so rebuilding the
    distributions per candidate repeated cubicly more work than needed)."""

    weights: BenchmarkDimensions
    distributions: list[dict[str, float]]  # per DIMENSION_NAMES
    size: int

    @classmethod
    def of(
        cls, universe: list[CandidateInfo], weights: BenchmarkDimensions
    ) -> UniverseProfile:
        """The share distribution of `universe` per benchmark dimension."""
        return cls(
            weights=weights,
            distributions=[
                distribution_of([dimension_values(c.assessment)[i] for c in universe])
                for i in range(len(DIMENSION_NAMES))
            ],
            size=len(universe),
        )


# ---------------------------------------------------------------- loop endings


@dataclass(frozen=True)
class StopDecision:
    """A loop-ending decision taken by the strategy — `message` is the exact
    log line, rendered by the caller."""

    reason: Literal["sufficient", "budget"]
    message: str


@dataclass(frozen=True)
class TerminationReason:
    """How the validation loop ended (PRD §61-66): one explicit fact from
    which the build outcome's insufficiency flag, warnings and budget marker
    are derived — never three overlapping signals."""

    reason: Literal["pool_exhausted", "sufficient", "budget"]
    validated_count: int
    valid_count: int
    remaining: int

    def insufficient(self, requested: int) -> bool:
        """Fewer VALID tasks than requested, whatever ended the loop — the
        historical `len(valid) < requested` flag."""
        return self.valid_count < requested

    def warnings(self, requested: int) -> list[str]:
        """Human-readable warnings for an undersized build (PRD §64); empty
        list = no insufficiency."""
        if self.valid_count >= requested:
            return []
        warnings = ["Low sample size."]
        if self.reason == "budget":
            warnings.append(
                f"Validation budget stopped at {self.validated_count} candidates."
            )
        return warnings


def _stop_message(valid_count: int, requested: int, remaining: int) -> str:
    return (
        f"stopping early: {valid_count} valid task(s) cover size "
        f"{requested} — {remaining} candidate(s) left "
        "unvalidated; smaller sampling pool"
    )


# ------------------------------------------------------------------ strategies


class ValidationStrategy(Protocol):
    """The mode-specific half of the validation loop (PRD §25)."""

    enforces_min_size: bool

    def filter_pool(
        self, pool: list[CandidateInfo], candidates: list[CandidateInfo]
    ) -> list[CandidateInfo]:
        """Restrict the pre-filtered candidate pool before any validation
        work (`candidates` is the full universe, for diagnostics)."""
        ...

    def pick(
        self,
        remaining: list[CandidateInfo],
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
    ) -> CandidateInfo:
        """The next candidate to validate."""
        ...

    def should_stop(
        self,
        valid_count: int,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> StopDecision | None:
        """A StopDecision when the strategy's stop condition is met, else
        None (the pool and the budget may still end the loop)."""
        ...

    def describe(self, candidate: CandidateInfo) -> str:
        """Progress-line suffix after "PR #N" ("" for a bare line)."""
        ...

    def note_reprioritization(
        self,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> str | None:
        """Optional queue announcement after a VALID result; a log line or
        None. Owns whatever steering state it needs across calls."""
        ...


class AdaptiveStrategy:
    """task_mining.size_policy == "adaptive" (PRD §25-29, §61-66): the pool is
    class-filtered (task_mining.validation.include, default high+medium),
    candidates are validated in coverage-aware priority order (HIGH → MEDIUM →
    EXPERIMENTAL, then 0.65×score + 0.35×coverage need, re-picked after every
    validation), the loop stops at requested size + min_representativeness,
    and the INSUFFICIENT_BENCHMARK gate applies."""

    enforces_min_size = True

    def __init__(self, cfg: RepoBenchConfig, requested: int) -> None:
        self._requested = requested
        self._include = cfg.task_mining.validation.include
        self._min_representativeness = cfg.benchmark.min_representativeness
        self._last_deficit: tuple[str, str] | None = None

    def filter_pool(
        self, pool: list[CandidateInfo], candidates: list[CandidateInfo]
    ) -> list[CandidateInfo]:
        # Adaptive class filter (PRD §33): the validation pool starts at HIGH +
        # MEDIUM confidence unless task_mining.validation.include says
        # otherwise — experimental candidates stay eligible, just out of the
        # default pool.
        include = self._include or ["high", "medium"]
        tokens = {klass.config_token for klass in CandidateClass}
        unknown = [name for name in include if name not in tokens]
        if unknown:
            raise UsageError(
                "task_mining.validation.include has unknown class(es) "
                f"{unknown} — valid: {sorted(tokens)}"
            )
        by_token = {klass.config_token: klass for klass in CandidateClass}
        allowed_classes = {by_token[name] for name in include}
        filtered = [
            c for c in pool if c.assessment.candidate_class in allowed_classes
        ]
        if not filtered:
            present = sorted(
                {
                    c.assessment.candidate_class.value
                    for c in candidates
                    if c.assessment.candidate_class is not None
                }
            )
            raise UsageError(
                f"no candidates in class(es) {include} — classes present in this "
                f"repository: {present or 'none'} (re-run `repobench analyze` "
                "with size_policy: adaptive to score candidates)"
            )
        return filtered

    def pick(
        self,
        remaining: list[CandidateInfo],
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
    ) -> CandidateInfo:
        return min(
            remaining,
            key=lambda c: (
                class_rank(c),
                -validation_priority(
                    c.assessment.candidate_score,
                    coverage_need(c.assessment, valid_assessments, profile),
                ),
                c.candidate_id,
            ),
        )

    def should_stop(
        self,
        valid_count: int,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> StopDecision | None:
        # PRD §26/§61: adaptive builds stop once the requested size is covered
        # AND the representativeness threshold (when configured) is met —
        # never validate 180 candidates after 24 already suffice.
        if valid_count < self._requested:
            return None
        if (
            self._min_representativeness is not None
            and representativeness(valid_assessments, profile)
            < self._min_representativeness
        ):
            return None
        return StopDecision(
            reason="sufficient",
            message=_stop_message(valid_count, self._requested, len(remaining)),
        )

    def describe(self, candidate: CandidateInfo) -> str:
        # PRD §60: score and class ride the progress line so the adaptive
        # ordering is visible while it happens. Unscored candidates (None
        # class ranks as medium) keep the plain line.
        score = candidate.assessment.candidate_score
        if score is None:
            return ""
        klass = candidate.assessment.candidate_class or CandidateClass.MEDIUM_CONFIDENCE
        return (
            f" · score {score:.2f} · {klass.short_label} · "
            f"{candidate.assessment.task_type.value}"
        )

    def note_reprioritization(
        self,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> str | None:
        # PRD §28/§60: announce the queue steering only when the top coverage
        # gap actually moves — never per pick.
        deficit = top_deficit(valid_assessments, profile)
        line: str | None = None
        if deficit is not None and deficit != self._last_deficit and remaining:
            dimension, key = deficit
            line = f"coverage need: {key} ({dimension}) — prioritizing {key} candidates"
        self._last_deficit = deficit
        return line


class HardCapsStrategy:
    """Legacy hard_caps / shadow semantics (PRD §34): the pool validates in
    its given order, `--stop-when-sufficient` stays the opt-in early stop,
    and the historical "build with what validated" behavior (no class filter,
    no minimum-size gate) is unchanged."""

    enforces_min_size = False

    def __init__(self, requested: int, stop_early: bool) -> None:
        self._requested = requested
        self._stop_early = stop_early

    def filter_pool(
        self, pool: list[CandidateInfo], candidates: list[CandidateInfo]
    ) -> list[CandidateInfo]:
        return pool

    def pick(
        self,
        remaining: list[CandidateInfo],
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
    ) -> CandidateInfo:
        return remaining[0]

    def should_stop(
        self,
        valid_count: int,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> StopDecision | None:
        if not self._stop_early or valid_count < self._requested:
            return None
        return StopDecision(
            reason="sufficient",
            message=_stop_message(valid_count, self._requested, len(remaining)),
        )

    def describe(self, candidate: CandidateInfo) -> str:
        return ""

    def note_reprioritization(
        self,
        valid_assessments: list[Assessment],
        profile: UniverseProfile,
        remaining: list[CandidateInfo],
    ) -> str | None:
        return None


def strategy_for(
    cfg: RepoBenchConfig, requested: int, stop_when_sufficient: bool
) -> ValidationStrategy:
    """The validation strategy for the configured mining mode (PRD §25):
    shadow and hard_caps keep the legacy semantics, adaptive gets the
    coverage-aware queue."""
    if cfg.task_mining.size_policy == "adaptive":
        return AdaptiveStrategy(cfg, requested)
    return HardCapsStrategy(requested, stop_when_sufficient)
