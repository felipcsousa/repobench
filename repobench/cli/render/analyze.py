"""Analyze renderers (PRD §30-31, §51, §59, §77) plus the config/subproject
summary lines surfaced by `init` (issue #34)."""

from __future__ import annotations

from pathlib import Path

from repobench.cli.render.base import WARN, echo, kv
from repobench.config import RepoBenchConfig, detect_subprojects
from repobench.core.types import RejectionCode, TaskStatus


def render_analyze_summary_v2(
    outcome, suggested_size: int, lookback_note: str
) -> None:  # noqa: ANN001 - AnalyzeOutcome (avoids import cycle)
    """The PRD §30/§59 analyze summary: candidate funnel, class counts, scope
    profile with cap pressure, benchmarkability and an honestly-labeled VALID
    estimate. Sections whose data is absent (None models, zero classes under
    legacy hard_caps mining) are skipped — never rendered as invented zeros."""
    funnel = getattr(outcome, "funnel", None)
    benchmarkability = getattr(outcome, "benchmarkability", None)
    scope_profile = getattr(outcome, "scope_profile", None)
    summary = outcome.summary
    echo("Repository Analysis")
    echo("────────────────────────────────────")
    echo("")
    kv("Merged PRs", f"{outcome.merged_prs}")
    if funnel is not None:
        kv("Relevant human changes", f"{funnel.human_changes}")
        kv("With implementation changes", f"{funnel.with_implementation}")
        kv("With verifier evidence", f"{funnel.with_verifier_evidence}")
        kv("With usable instructions", f"{funnel.with_instruction}")
    echo("")
    if funnel is None and summary.workload.task_type:
        # Legacy hard_caps outcome (no funnel): the workload shares are the only
        # distribution view available, so the degrade path keeps the lines the
        # v1 renderer carried instead of dropping the section entirely.
        echo("Workload")
        echo("")
        shares: dict[str, float] = {}
        for key, share in summary.workload.task_type.items():
            shares[key] = shares.get(key, 0.0) + share
        for task_type, share in sorted(shares.items(), key=lambda item: -item[1]):
            echo(f"{task_type:<28}{round(share * 100):>3.0f}%")
        echo("")
    scored = (
        funnel is not None
        and funnel.high_confidence + funnel.medium_confidence + funnel.experimental > 0
    )
    if scored:
        echo("Candidate opportunity")
        echo("")
        kv("High confidence", f"{funnel.high_confidence}")
        kv("Medium confidence", f"{funnel.medium_confidence}")
        kv("Experimental", f"{funnel.experimental}")
        echo("")
    if scope_profile is not None and scope_profile.n > 0:
        if scope_profile.cap_pressure is not None and scope_profile.cap_pressure >= 50:
            echo("Large changes are common here.")
        kv("Median implementation LOC", f"{scope_profile.p50:,.0f}")
        kv("P75", f"{scope_profile.p75:,.0f}")
        kv("P90", f"{scope_profile.p90:,.0f}")
        echo("")
    if benchmarkability is not None:
        echo("Benchmarkability")
        echo("")
        kv("Band", f"{benchmarkability.band} · {benchmarkability.score}/100")
        components = benchmarkability.components
        kv(
            "Components",
            " · ".join(f"{key.replace('_', ' ')} {value}" for key, value in components.items()),
        )
        for note in benchmarkability.notes:
            echo(f"  {note}")
        echo("")
    if scored:
        # Rough estimate, honestly labeled (PRD §59): validation decides — these
        # bounds only say where the VALID count usually lands per class mix.
        # Only HIGH+MEDIUM calibrate the estimate: EXPERIMENTAL candidates are
        # exactly the ones we do not yet trust to convert, so a pool of zero
        # calibrated candidates renders no invented range.
        pool = funnel.high_confidence + funnel.medium_confidence
        if pool > 0:
            echo("Estimated VALID tasks")
            echo("")
            kv("Range", f"{round(0.55 * pool)}–{round(0.90 * pool)} (rough estimate from candidate classes)")
            echo("")
    echo("Requested benchmark size")
    echo("")
    kv("Tasks", f"{suggested_size}")
    echo("")
    echo(f"{lookback_note}")
    echo("No inference tokens were consumed.")


def render_zero_candidate_experience(
    outcome, lookback_days: int
) -> None:  # noqa: ANN001 - AnalyzeOutcome
    """The PRD §31 zero-candidate experience: a dead end must teach the way out.
    Blocker shares come from real FILTERED counts; every suggested action is
    emitted only when a real count backs it — nothing is invented."""
    candidates = outcome.candidates
    filtered = [c for c in candidates if c.status is TaskStatus.FILTERED]
    echo("")
    echo("No candidates found.")
    echo("")
    if not filtered:
        echo("No merged human PRs survived to the mining stage — try a")
        echo("repository with richer engineering history.")
        return
    counts: dict[RejectionCode, int] = {}
    for candidate in filtered:
        assert candidate.rejection_code is not None  # FILTERED always carries one
        counts[candidate.rejection_code] = counts.get(candidate.rejection_code, 0) + 1
    echo("Primary blockers:")
    echo("")
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].value))
    for i, (code, count) in enumerate(ordered):
        label = "Other" if i == len(ordered) - 1 and len(ordered) > 1 else code.value
        kv(label, f"{round(100 * count / len(filtered))}%")
    echo("")
    echo("What to try:")
    echo("")
    lookback = lookback_days
    no_instruction = counts.get(RejectionCode.NO_INSTRUCTION, 0)
    no_test = counts.get(RejectionCode.NO_TEST_CHANGE, 0)
    echo(f"1. Increase repository.lookback_days (currently {lookback})")
    if no_instruction:
        echo(
            f"2. Enable instruction_generation (tier D) — {no_instruction} PR(s) "
            "filtered by NO_INSTRUCTION could be recovered"
        )
    if no_test:
        echo(
            f"3. Review project.test_command / custom test paths — {no_test} PR(s) "
            "filtered by NO_TEST_CHANGE"
        )
    if not no_instruction and not no_test:
        echo("2. Try a repository with richer engineering history")


def render_shadow_delta(outcome) -> None:  # noqa: ANN001 - AnalyzeOutcome
    """Shadow mode report (PRD §77 Phase B): hard_caps still controls; the delta
    is what adaptive WOULD decide — the calibration data for flipping the
    default later. Every count comes from the two real mining passes."""
    delta = outcome.shadow_delta
    echo("")
    echo("Shadow mode — adaptive mining evaluated alongside hard_caps")
    echo("")
    kv("Controlling policy", delta.controlling_policy)
    kv("Pool under hard_caps", f"{delta.hard_caps_pool}")
    kv("Pool under adaptive", f"{delta.adaptive_pool}")
    kv(
        "Additional candidates",
        f"+{delta.additional_candidates} "
        f"({delta.recovered_from_size_caps} recovered from size caps)",
    )
    if delta.by_complexity:
        sizes = " · ".join(f"{key}×{count}" for key, count in delta.by_complexity.items())
        kv("Additional by size", sizes)
    if delta.adaptive_class_counts:
        classes = " · ".join(
            f"{key}×{count}" for key, count in delta.adaptive_class_counts.items()
        )
        kv("Adaptive classes", classes)
    echo("")
    echo("Shadow candidates never reach storage or benchmarks — hard_caps decides.")
    echo("")


def render_merge_style_warnings(outcome) -> None:  # noqa: ANN001 - AnalyzeOutcome
    """Issue #31 field-credibility notes: squash-merged PRs are mined from
    commit subjects, and — only when gh ground truth exists — recall low enough
    to prove some history is invisible (rebase merges carry no PR number in git
    at all). Without gh, nothing is claimed about recall."""
    styles = getattr(outcome, "merge_styles", None)
    if styles is not None and styles.squash:
        plural = "s" if styles.squash != 1 else ""
        echo(
            f"{WARN} merge style: {styles.squash} squash-merged PR{plural} mined "
            "from commit subjects (#N) — no merge commits for them exist"
        )
    recall_total = getattr(outcome, "recall_total", None)
    if not recall_total:  # None (no gh) or 0 merged PRs — no honest recall claim
        return
    mined = outcome.merged_prs
    if recall_total > 0 and round(100 * mined / recall_total) < 70:
        missing = recall_total - mined
        plural = "s" if missing != 1 else ""
        echo(
            f"{WARN} low recall: {missing} of {recall_total} merged PR"
            f"{plural} in the window are invisible to mining — PRs merged by "
            "rebase carry no PR number in git at all"
        )


def render_public_repository_warning() -> None:
    """The PRD §51 contamination warning block, verbatim in spirit."""
    echo("")
    echo("⚠ PUBLIC REPOSITORY")
    echo("")
    echo("This benchmark cannot guarantee that the tested model or agent has")
    echo("never seen the repository, its issues or the solution.")
    echo("")
    echo("Results measure practical performance, not contamination-free capability.")
    echo("")


def subproject_summary_lines(root: Path) -> list[str]:
    """Issue #34: one honest line per detected sub-project — the backend the
    old root-only detection silently ignored. Empty when none exist, so callers
    never print a monorepo header for a single-project repo. A sub-project
    without a test command says so instead of inventing one."""
    projects = detect_subprojects(root)
    if not projects:
        return []
    lines = [
        "sub-projects detected (one command set benchmarks — project.cwd picks where it runs):"
    ]
    for project in projects:
        cfg = project.config
        identity = [project.path, cfg.language or "unknown"]
        if cfg.package_manager:
            identity.append(cfg.package_manager)
        if cfg.install_command:
            identity.append(f"install: {cfg.install_command}")
        if cfg.test_command:
            identity.append(f"test: {cfg.test_command}")
        else:
            identity.append("no test command — set project.test_command or project.cwd")
        lines.append("  " + " · ".join(identity))
    return lines


def config_summary_lines(cfg: RepoBenchConfig, root: Path) -> list[str]:
    project = cfg.project
    lines = [
        f"repo:      {root}",
        f"language:  {project.language or 'not detected'}",
        f"packages:  {project.package_manager or 'not detected'}",
    ]
    if project.cwd:
        # Issue #34: the monorepo knob is part of the summary so a configured
        # sub-directory is always visible next to the commands it redirects.
        lines.append(f"cwd:       {project.cwd}")
    if project.install_command:
        lines.append(f"install:   {project.install_command}")
    if project.test_command:
        lines.append(f"test:      {project.test_command}")
    elif project.language:
        # Issue #34 honesty: say so instead of implying `npm test` exists —
        # an invented suggestion guarantees BASELINE_BROKEN at first build.
        lines.append("test:      none detected — set project.test_command in repobench.yml")
    # Issue #34: surface every sub-project so the ignored backend is seen
    # before the first build, not after a guaranteed-broken run.
    lines.extend(subproject_summary_lines(root))
    lines.append("edit repobench.yml to adjust commands, targets and benchmark size.")
    return lines
