"""Regression net for the render package split (Finding 12) and the stored-class
inspect recommendation (Finding 7).

The split must be invisible outside repobench.cli.render: the package
re-exports the entire historical module surface, so `from repobench.cli import
render` + `render.anything` and `from repobench.cli.render import echo, kv`
keep working unchanged. The inspect Recommendation branches on the STORED
candidate_class (classified with the configured thresholds at mining time) —
never on renderer-side score thresholds."""

from __future__ import annotations

import pytest

import repobench.cli.render as render
from repobench.cli.render import render_candidate_inspect
from repobench.core.types import (
    Assessment,
    CandidateClass,
    CandidateInfo,
    Complexity,
    PRInfo,
)


def test_package_reexports_base_surface() -> None:
    assert callable(render.echo)
    assert callable(render.fail)
    assert callable(render.kv)
    assert (render.OK, render.MISS, render.WARN) == ("✓", "✗", "⚠")
    assert render.console is not None


def test_package_reexports_every_domain_renderer() -> None:
    for name in (
        # analyze
        "render_analyze_summary_v2",
        "render_zero_candidate_experience",
        "render_shadow_delta",
        "render_merge_style_warnings",
        "render_public_repository_warning",
        "config_summary_lines",
        "subproject_summary_lines",
        # candidates
        "render_candidates_table",
        "render_candidate_inspect",
        "render_pr_diagnostics",
        # benchmark
        "render_benchmark_build",
        "render_benchmark_refresh",
        # runs (render_task_build_line is wired into benchmark builds via app.py)
        "render_run_preview",
        "render_progress",
        "render_run_summary",
        "render_task_build_line",
        "render_runs_table",
        "render_run_show",
        "render_clean_plan",
        # targets
        "render_targets_table",
        "provider_label",
        "render_capability_row",
        # compare
        "render_compare",
    ):
        assert callable(getattr(render, name)), name


def test_from_imports_keep_working() -> None:
    from repobench.cli.render import (  # noqa: F401 - import-surface regression
        MISS,
        OK,
        _target_pricing_label,
        echo,
        kv,
        render_benchmark_build,
    )

    assert callable(echo) and callable(kv) and callable(render_benchmark_build)


def _candidate(
    score: float | None, klass: CandidateClass | None, complexity: Complexity
) -> CandidateInfo:
    return CandidateInfo(
        candidate_id="c_1",
        pr=PRInfo(number=1),
        assessment=Assessment(
            candidate_score=score,
            candidate_class=klass,
            complexity=complexity,
        ),
    )


@pytest.mark.parametrize(
    ("score", "klass", "complexity", "recommendation"),
    [
        # Stored HIGH class wins over a score the old renderer would have
        # re-derived as experimental (thresholds live at mining time only).
        (0.55, CandidateClass.HIGH_CONFIDENCE, Complexity.LARGE, "Strong candidate despite large diff."),
        (0.95, CandidateClass.HIGH_CONFIDENCE, Complexity.SMALL, "Reasonable candidate."),
        (0.90, CandidateClass.EXPERIMENTAL, Complexity.SMALL, "Experimental — validate only after better candidates."),
        (0.70, CandidateClass.MEDIUM_CONFIDENCE, Complexity.MEDIUM, "Reasonable candidate."),
    ],
)
def test_inspect_recommendation_branches_on_stored_class(
    score: float,
    klass: CandidateClass,
    complexity: Complexity,
    recommendation: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_candidate_inspect(_candidate(score, klass, complexity))
    out = capsys.readouterr().out
    assert "Recommendation" in out
    assert recommendation in out


def test_inspect_omits_recommendation_for_unscored_candidate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_candidate_inspect(_candidate(None, None, Complexity.MEDIUM))
    out = capsys.readouterr().out
    assert "Legacy hard_caps mining" in out  # the honest unscored note
    assert "Recommendation" not in out  # nothing to recommend — omitted entirely
