"""Adaptive validation priority, budget and graceful insufficiency (PRD §25-29, §61-66).

Unit tests exercise the pure priority API with hand-built candidates; the
build-level tests stub package reconstruction and validation so the QUEUE
semantics — class order, coverage-aware re-picking, budget caps, stop
conditions, INSUFFICIENT_BENCHMARK — are observable without paying real
validation (which test_benchmark.py already covers end-to-end).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repobench.benchmark.priority import (
    class_rank,
    coverage_need,
    order_pool,
    representativeness,
    validation_priority,
)
from repobench.benchmark.reuse import task_id_for
from repobench.benchmark.sampling import DIMENSION_NAMES, distribution_of
from repobench.cli import builds as builds_module
from repobench.cli.builds import build_benchmark
from repobench.config import RepoBenchConfig
from repobench.core.errors import UsageError
from repobench.core.paths import ProjectPaths
from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    PRInfo,
    TaskMetadata,
    TaskPackage,
    TaskStatus,
    TaskType,
)
from repobench.storage.db import Storage
from repobench.tasks.leakage import LeakageReport
from repobench.validation.pipeline import TaskValidationReport


def _candidate(
    number: int,
    *,
    task_type: TaskType = TaskType.BUGFIX,
    score: float | None = None,
    candidate_class: CandidateClass | None = None,
    subsystem: str = "math",
) -> CandidateInfo:
    """A scored adaptive-shaped candidate — hand-built, no repository behind it
    (the build-level tests below stub package reconstruction)."""
    return CandidateInfo(
        candidate_id=f"c{number:04d}",
        pr=PRInfo(
            number=number,
            title=f"PR {number}",
            base_sha=f"b{number:04d}" + "0" * 32,
            head_sha=f"h{number:04d}" + "0" * 32,
            merge_sha=f"m{number:04d}" + "0" * 32,
        ),
        status=TaskStatus.DISCOVERED,
        assessment=Assessment(
            task_type=task_type,
            subsystem=subsystem,
            candidate_score=score,
            candidate_class=candidate_class,
            instruction="do the thing",
            instruction_confidence="B",
            test_files=2,
            test_loc=30,
            implementation_loc=40,
            implementation_files=2,
        ),
    )


_DIMS = RepoBenchConfig().benchmark.dimensions


# ------------------------------------------------------------ pure priority API


def test_coverage_need_boosts_underrepresented_type() -> None:
    """PRD §28: with bugfix saturated and integration missing, the integration
    candidate carries the task-type deficit and the bugfix carries none."""
    universe = [
        _candidate(1, task_type=TaskType.BUGFIX),
        _candidate(2, task_type=TaskType.INTEGRATION),
        _candidate(3, task_type=TaskType.INTEGRATION),
    ]
    validated = [universe[0].assessment]  # the one bugfix already validated

    bugfix_need = coverage_need(universe[0].assessment, validated, universe, _DIMS)
    integration_need = coverage_need(universe[1].assessment, validated, universe, _DIMS)

    assert bugfix_need == 0.0  # universe 1/3 vs valid 1.0 — no deficit left
    # integration: task_type weight 0.3 × deficit (2/3 − 0) = 0.2 (subsystem and
    # complexity are shared across all candidates, so their deficits cancel).
    assert integration_need == pytest.approx(0.2)


def test_validation_priority_weights_and_none_score() -> None:
    """PRD §28: 0.65×score + 0.35×need; a missing score counts as zero —
    absent evidence never invents quality."""
    assert validation_priority(1.0, 0.0) == 0.65
    assert validation_priority(0.0, 1.0) == 0.35
    assert validation_priority(None, 0.5) == round(0.35 * 0.5, 2)


def test_prd_29_integration_can_outrank_higher_score_bugfix() -> None:
    """The PRD §29 example: A scores .93 (bugfix), B scores .86 (integration);
    with integration under-represented, B validates before A."""
    a = _candidate(1, task_type=TaskType.BUGFIX, score=0.93)
    b = _candidate(2, task_type=TaskType.INTEGRATION, score=0.86)
    universe = [a, b, _candidate(3, task_type=TaskType.INTEGRATION)]
    validated = [_candidate(9, task_type=TaskType.BUGFIX).assessment]

    priority_a = validation_priority(
        a.assessment.candidate_score,
        coverage_need(a.assessment, validated, universe, _DIMS),
    )
    priority_b = validation_priority(
        b.assessment.candidate_score,
        coverage_need(b.assessment, validated, universe, _DIMS),
    )
    assert priority_b > priority_a


def test_class_rank_orders_high_medium_experimental() -> None:
    assert (
        class_rank(_candidate(1, candidate_class=CandidateClass.HIGH_CONFIDENCE))
        < class_rank(_candidate(2, candidate_class=CandidateClass.MEDIUM_CONFIDENCE))
        < class_rank(_candidate(3, candidate_class=CandidateClass.EXPERIMENTAL))
    )
    # Unscored (hard_caps) ranks as medium — never better than a known HIGH.
    assert class_rank(_candidate(4)) == class_rank(
        _candidate(5, candidate_class=CandidateClass.MEDIUM_CONFIDENCE)
    )


def test_order_pool_deterministic_with_candidate_id_tiebreak() -> None:
    pool = [
        _candidate(3, score=0.5, candidate_class=CandidateClass.HIGH_CONFIDENCE),
        _candidate(1, score=0.5, candidate_class=CandidateClass.HIGH_CONFIDENCE),
        _candidate(2, score=0.9, candidate_class=CandidateClass.MEDIUM_CONFIDENCE),
    ]
    universe = list(pool)
    first = order_pool(pool, universe, _DIMS, [])
    again = order_pool(pool, universe, _DIMS, [])
    assert first == again
    # class first (HIGH before MEDIUM), then priority desc, then candidate_id.
    assert [c.pr.number for c in first] == [1, 3, 2]


def test_representativeness_bounds() -> None:
    candidates = [_candidate(1), _candidate(2), _candidate(3)]
    assert representativeness([], candidates, _DIMS) == 0.0
    # every candidate identical → the valid distribution equals the universe's.
    assert representativeness(
        [c.assessment for c in candidates], candidates, _DIMS
    ) == pytest.approx(100.0)


# ------------------------------------------------------- build-level (stubbed)


def _clean_leakage() -> LeakageReport:
    return LeakageReport(
        checks={
            "history_isolation": True,
            "gold_isolation": True,
            "verifier_isolation": True,
            "github_credentials": True,
            "network_isolation": False,
        },
        score=78,
        risk="LOW",
        evidence=[],
    )


def _adaptive_cfg(**benchmark_overrides: object) -> RepoBenchConfig:
    cfg = RepoBenchConfig()
    cfg.task_mining.size_policy = "adaptive"
    for key, value in benchmark_overrides.items():
        setattr(cfg.benchmark, key, value)
    return cfg


@pytest.fixture
def stubbed_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Package reconstruction, leakage and validation replaced by stubs that
    record which PR validated in which order; the queue logic under test stays
    fully real. Returns (install(outcomes), validated_order, paths)."""
    validated_order: list[int] = []

    def fake_package_for(repo, candidate, paths):  # noqa: ANN001
        directory = paths.task_dir(task_id_for(candidate))
        directory.mkdir(parents=True, exist_ok=True)
        metadata = TaskMetadata(
            task_id=task_id_for(candidate),
            pr_number=candidate.pr.number,
            title=candidate.pr.title,
            base_sha=candidate.pr.base_sha or f"base{candidate.pr.number}",
            gold_sha=candidate.pr.merge_sha or f"gold{candidate.pr.number}",
            assessment=candidate.assessment,
        )
        return TaskPackage(
            task_id=metadata.task_id, directory=directory, metadata=metadata
        )

    def make_validator(outcomes: dict[int, str]):
        class StubValidator:
            def __init__(self, project, workspaces_root):  # noqa: ANN001
                pass

            def validate(self, package, *, leakages=None):  # noqa: ANN001
                pr = package.metadata.pr_number
                validated_order.append(pr)
                status = TaskStatus(outcomes.get(pr, "VALID"))
                return TaskValidationReport(
                    task_id=package.task_id,
                    status=status,
                    rejection_code=None
                    if status is TaskStatus.VALID
                    else None,
                    checks=[],
                )

        return StubValidator

    monkeypatch.setattr(builds_module, "_package_for", fake_package_for)
    monkeypatch.setattr(
        builds_module, "scan_base_archive", lambda *a, **k: _clean_leakage()
    )
    holder: dict[str, object] = {}

    def install(outcomes: dict[int, str]) -> None:
        validated_order.clear()
        holder["validator"] = make_validator(outcomes)
        monkeypatch.setattr(
            builds_module, "TaskValidator", holder["validator"]  # type: ignore[arg-type]
        )

    return install, validated_order


def _git_init(root: Path) -> Path:
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "--quiet", "--allow-empty", "-m", "init"],
        cwd=root, check=True, capture_output=True,
    )
    return root


def _adaptive_run(
    tmp_path: Path,
    cfg: RepoBenchConfig,
    candidates: list[CandidateInfo],
    install,  # noqa: ANN001
    outcomes: dict[int, str],
    **build_kwargs: object,
):
    root = _git_init(tmp_path / "repo")
    paths = ProjectPaths(root)
    paths.ensure()
    storage = Storage(tmp_path / "state.db")
    for candidate in candidates:
        storage.save_candidate(candidate)
    install(outcomes)
    lines: list[str] = []
    outcome = build_benchmark(root, cfg, storage, log=lines.append, **build_kwargs)
    return outcome, lines, storage


def _scored(
    number: int,
    *,
    score: float,
    klass: CandidateClass,
    task_type: TaskType = TaskType.BUGFIX,
) -> CandidateInfo:
    return _candidate(number, task_type=task_type, score=score, candidate_class=klass)


def test_adaptive_build_stops_when_sufficient(
    tmp_path: Path, stubbed_build
) -> None:
    """PRD §26: with 3 candidates and a requested size of 2, the loop stops at
    2 VALID — the third never pays validation."""
    install, order = stubbed_build
    candidates = [
        _scored(1, score=0.95, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(2, score=0.90, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(3, score=0.85, klass=CandidateClass.HIGH_CONFIDENCE),
    ]
    cfg = _adaptive_cfg(min_size=1)
    outcome, lines, _storage = _adaptive_run(
        tmp_path, cfg, candidates, install, {}, size=2
    )
    assert len(outcome.valid) == 2
    assert order == [1, 2]  # priority order: score descending
    assert outcome.insufficient is False
    assert any("stopping early" in line for line in lines)
    # PRD §60: the progress line carries score and class.
    assert any("score 0.95" in line and "HIGH" in line for line in lines)


def test_adaptive_build_budget_caps_validation(
    tmp_path: Path, stubbed_build
) -> None:
    """PRD §63: max_candidates bounds the work even when nothing is VALID yet."""
    install, order = stubbed_build
    candidates = [
        _scored(1, score=0.95, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(2, score=0.90, klass=CandidateClass.HIGH_CONFIDENCE),
    ]
    cfg = _adaptive_cfg(min_size=1)
    cfg.benchmark.validation_budget.max_candidates = 1
    outcome, lines, _storage = _adaptive_run(
        tmp_path, cfg, candidates, install, {}, size=2
    )
    assert order == [1]
    assert outcome.budget_stopped_at == 1
    assert any("validation budget reached" in line for line in lines)


def test_adaptive_build_insufficient_benchmark_and_graceful_path(
    tmp_path: Path, stubbed_build
) -> None:
    """PRD §64-65: below benchmark.min_size the build refuses honestly; at or
    above it, a smaller-than-requested benchmark still ships with warnings."""
    install, order = stubbed_build
    candidates = [
        _scored(1, score=0.95, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(2, score=0.90, klass=CandidateClass.HIGH_CONFIDENCE),
    ]
    # min_size 3 > 2 achievable VALID → INSUFFICIENT_BENCHMARK.
    cfg = _adaptive_cfg(min_size=3)
    with pytest.raises(UsageError) as excinfo:
        _adaptive_run(tmp_path / "a", cfg, candidates, install, {}, size=5)
    assert "INSUFFICIENT_BENCHMARK" in str(excinfo.value)

    # min_size 1 ≤ 2 VALID → graceful: builds with 2 of 5, flagged.
    cfg = _adaptive_cfg(min_size=1)
    outcome, lines, _storage = _adaptive_run(
        tmp_path / "b", cfg, candidates, install, {}, size=5
    )
    assert len(outcome.valid) == 2
    assert outcome.insufficient is True
    assert "Low sample size." in outcome.build_warnings
    assert any("building with 2 tasks" in line for line in lines)


def test_adaptive_pool_class_filter(tmp_path: Path, stubbed_build) -> None:
    """PRD §33: default include = high+medium; an explicit include=["high"]
    keeps experimental/medium candidates out of the pool entirely."""
    install, order = stubbed_build
    candidates = [
        _scored(1, score=0.95, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(2, score=0.60, klass=CandidateClass.MEDIUM_CONFIDENCE),
        _scored(3, score=0.40, klass=CandidateClass.EXPERIMENTAL),
    ]
    # experimental candidate is LOW_PRIORITY at mining time — pool-eligible
    # only when explicitly included.
    candidates[2] = candidates[2].model_copy(
        update={"status": TaskStatus.LOW_PRIORITY}
    )
    cfg = _adaptive_cfg(min_size=1)
    cfg.task_mining.validation.include = ["high"]
    outcome, _lines, _storage = _adaptive_run(
        tmp_path, cfg, candidates, install, {}, size=1
    )
    assert order == [1]  # medium and experimental never validated

    # include=["experimental"] reaches the LOW_PRIORITY candidate.
    install2, order2 = stubbed_build  # fresh fixture instance
    cfg2 = _adaptive_cfg(min_size=1)
    cfg2.task_mining.validation.include = ["experimental"]
    outcome2, _l2, _s2 = _adaptive_run(
        tmp_path / "c", cfg2, candidates, install2, {}, size=1
    )
    assert order2 == [3]


def test_adaptive_queue_coverage_boost_steers_pick_order(
    tmp_path: Path, stubbed_build
) -> None:
    """PRD §29 end-to-end: B (.78, integration) validates before A (.80,
    bugfix) because the universe leans integration 2:1 and nothing validated
    yet — the coverage term outweighs the small score gap."""
    install, order = stubbed_build
    a = _scored(1, score=0.80, klass=CandidateClass.HIGH_CONFIDENCE)
    b = _scored(2, score=0.78, klass=CandidateClass.HIGH_CONFIDENCE,
                task_type=TaskType.INTEGRATION)
    universe_extra = _scored(3, score=0.70, klass=CandidateClass.HIGH_CONFIDENCE,
                             task_type=TaskType.INTEGRATION)
    cfg = _adaptive_cfg(min_size=1)
    outcome, _lines, _storage = _adaptive_run(
        tmp_path, cfg, [a, b, universe_extra], install, {}, size=1
    )
    assert order[0] == 2  # B first despite the lower score


def test_hard_caps_build_keeps_fixed_order_and_no_budget(
    tmp_path: Path, stubbed_build
) -> None:
    """Parity: without size_policy adaptive, the pool order stays pr_number —
    the 0.95-score PR does NOT jump the queue — and every candidate validates."""
    install, order = stubbed_build
    candidates = [
        _scored(2, score=0.95, klass=CandidateClass.HIGH_CONFIDENCE),
        _scored(1, score=0.60, klass=CandidateClass.MEDIUM_CONFIDENCE),
    ]
    cfg = RepoBenchConfig()  # hard_caps default
    outcome, _lines, _storage = _adaptive_run(
        tmp_path, cfg, candidates, install, {}, size=1
    )
    assert order == [1, 2]  # storage order (pr_number), NOT priority
    assert outcome.budget_stopped_at is None
