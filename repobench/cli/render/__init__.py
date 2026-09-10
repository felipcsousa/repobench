"""Rich rendering for the RepoBench CLI (PRD §10, §88, §91-93, §96, §111).

All dynamic content is printed with markup disabled — repository paths and
task ids may contain characters Rich would otherwise interpret.

Split by domain (Finding 12): base plumbing, analyze, candidates, benchmark,
runs, targets and compare. This package re-exports the entire historical
module-level surface, so `from repobench.cli import render` + `render.anything`
and `from repobench.cli.render import echo, kv` keep working with no churn
outside the package.
"""

from __future__ import annotations

from repobench.cli.render.analyze import (
    config_summary_lines,
    render_analyze_summary_v2,
    render_merge_style_warnings,
    render_public_repository_warning,
    render_shadow_delta,
    render_zero_candidate_experience,
    subproject_summary_lines,
)
from repobench.cli.render.base import (
    MISS,
    OK,
    WARN,
    console,
    echo,
    fail,
    kv,
)
from repobench.cli.render.benchmark import (
    render_benchmark_build,
    render_benchmark_refresh,
)
from repobench.cli.render.candidates import (
    _decode_details,
    _fmt_score,
    render_candidate_inspect,
    render_candidates_table,
    render_pr_diagnostics,
)
from repobench.cli.render.compare import render_compare
from repobench.cli.render.runs import (
    _short_when,
    render_clean_plan,
    render_progress,
    render_run_preview,
    render_run_show,
    render_run_summary,
    render_runs_table,
    render_task_build_line,
)
from repobench.cli.render.targets import (
    _target_pricing_label,
    _target_provider,
    provider_label,
    render_capability_row,
    render_targets_table,
)

__all__ = [
    # base
    "MISS",
    "OK",
    "WARN",
    "console",
    "echo",
    "fail",
    "kv",
    # analyze
    "config_summary_lines",
    "render_analyze_summary_v2",
    "render_merge_style_warnings",
    "render_public_repository_warning",
    "render_shadow_delta",
    "render_zero_candidate_experience",
    "subproject_summary_lines",
    # candidates
    "_decode_details",
    "_fmt_score",
    "render_candidate_inspect",
    "render_candidates_table",
    "render_pr_diagnostics",
    # benchmark
    "render_benchmark_build",
    "render_benchmark_refresh",
    # runs
    "_short_when",
    "render_clean_plan",
    "render_progress",
    "render_run_preview",
    "render_run_show",
    "render_run_summary",
    "render_runs_table",
    "render_task_build_line",
    # targets
    "_target_pricing_label",
    "_target_provider",
    "provider_label",
    "render_capability_row",
    "render_targets_table",
    # compare
    "render_compare",
]
