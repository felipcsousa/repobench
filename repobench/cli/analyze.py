"""RepoBench Analyze — analyze repository workload from Git and GitHub history."""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from repobench.config import load_config
from repobench.logging import get_logger, setup_logging
from repobench.mining.candidates import discover_candidates
from repobench.models import Complexity, PRWorkloadInfo, PullRequest, TaskType
from repobench.storage.database import Database
from repobench.utils import (
    get_git_root,
    get_github_owner_repo,
    run_cmd_safe,
)

logger = get_logger("analyze")
console = Console()


# ── Classification helpers ─────────────────────────────────────────────────────

_BUGFIX_KEYWORDS = re.compile(
    r"\b(fix|bug|bugfix|regression|broken|incorrect|error|patch)\b", re.IGNORECASE
)
_FEATURE_KEYWORDS = re.compile(
    r"\b(feat|feature|add|support|implement|enhancement|new)\b", re.IGNORECASE
)

_BOT_AUTHORS = {"dependabot", "renovate", "snyk", "sonarcloud", "codecov", "github-actions"}


def _classify_task_type(title: str, labels: list[str]) -> tuple[TaskType, float]:
    """Classify task type from PR title and labels. Returns (type, confidence)."""
    # Priority 1: GitHub labels
    label_text = " ".join(labels).lower()
    if any(k in label_text for k in ("bug", "bugfix", "regression")):
        return TaskType.BUGFIX, 0.95
    if any(k in label_text for k in ("feature", "enhancement")):
        return TaskType.FEATURE, 0.95

    # Priority 2: Conventional commit / PR title
    title_lower = title.lower()
    if _BUGFIX_KEYWORDS.search(title_lower):
        return TaskType.BUGFIX, 0.80
    if _FEATURE_KEYWORDS.search(title_lower):
        return TaskType.FEATURE, 0.80

    # Priority 3: weak signal
    return TaskType.UNKNOWN, 0.30


def _detect_subsystem(changed_files: list[str]) -> str:
    """Detect subsystem from changed file paths."""
    if not changed_files:
        return "unknown"

    # Strategy: workspace/package detection, then stable directory
    for f in changed_files:
        parts = Path(f).parts
        if len(parts) >= 2:
            # Check for workspace patterns: packages/X, apps/X, services/X
            if parts[0] in ("packages", "apps", "services", "libs", "modules"):
                return parts[1]
            # Use first significant directory
            if parts[0] in ("src", "lib", "internal"):
                if len(parts) >= 2:
                    return parts[1]
            return parts[0]

    return "unknown"


def _compute_complexity(
    impl_loc: int,
    impl_files: int,
    packages_touched: int,
    test_loc: int,
) -> Complexity:
    """Compute relative complexity using weighted log heuristic."""
    score = (
        0.45 * math.log(max(impl_loc, 1))
        + 0.30 * math.log(max(impl_files, 1))
        + 0.15 * packages_touched
        + 0.10 * math.log(max(test_loc, 1))
    )
    # Normalize roughly (this is relative within the repository)
    if score < 3.5:
        return Complexity.SMALL
    elif score < 5.5:
        return Complexity.MEDIUM
    else:
        return Complexity.LARGE


def _is_automated_maintenance(pr: dict) -> bool:
    """Check if PR is automated maintenance (dependabot, lockfile-only, etc.)."""
    author = (pr.get("author") or "").lower()
    labels = [label.lower() for label in (pr.get("labels") or [])]

    # Bot authors
    if any(bot in author for bot in _BOT_AUTHORS):
        return True

    # Dependabot/Renovate labels
    if any(label in ("dependencies", "dependencies-updates") for label in labels):
        return True

    return False


def _is_doc_only(changed_files: list[str]) -> bool:
    """Check if PR only changes documentation."""
    doc_exts = {".md", ".rst", ".txt", ".adoc"}
    code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".sh"}
    non_code_dirs = {"docs", "doc", "documentation", ".github"}

    if not changed_files:
        return False

    for f in changed_files:
        ext = Path(f).suffix.lower()
        if ext in code_exts:
            return False
        top_dir = Path(f).parts[0] if Path(f).parts else ""
        if top_dir not in non_code_dirs and ext not in doc_exts:
            return False

    return True


def _detect_languages(changed_files: list[str]) -> list[str]:
    """Detect languages from file extensions."""
    ext_map = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
    }
    langs = set()
    for f in changed_files:
        ext = Path(f).suffix.lower()
        if ext in ext_map:
            langs.add(ext_map[ext])
    return sorted(langs)


def _extract_test_files(changed_files: list[str]) -> list[str]:
    """Extract test files from changed files."""
    from repobench.tasks.verifier import detect_test_files

    return detect_test_files(changed_files)


def _extract_implementation_files(changed_files: list[str]) -> list[str]:
    """Extract implementation (non-test) files."""
    test_files = set(_extract_test_files(changed_files))
    return [f for f in changed_files if f not in test_files]


def _get_linked_issue(body: str | None) -> tuple[int | None, str | None]:
    """Extract linked issue number from PR body."""
    if not body:
        return None, None
    match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
    if match:
        return int(match.group(1)), None
    return None, None


def _fetch_merged_prs(owner: str, repo: str, since: str, verbose: bool) -> list[dict]:
    """Fetch merged PRs from GitHub via gh CLI with pagination."""
    all_prs: list[dict] = []
    page = 1
    per_page = 100

    query = f"repo:{owner}/{repo} type:pr is:merged merged:>={since}"

    while True:
        cmd = [
            "gh",
            "api",
            "search/issues",
            "--method",
            "GET",
            "-f",
            f"q={query}",
            "-f",
            f"per_page={per_page}",
            "-f",
            f"page={page}",
            "--jq",
            (
                ".items[] | {number: .number, title: .title, body: .body, "
                "user: .user.login, labels: [.labels[].name], created_at: .created_at, "
                "pull_request: .pull_request}"
            ),
        ]

        success, stdout, stderr = run_cmd_safe(cmd, timeout=120)

        if not success:
            if verbose:
                logger.warning("gh api failed on page %d: %s", page, stderr)
            break

        try:
            # gh api search returns one JSON object per line
            items = []
            for line in stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        except json.JSONDecodeError:
            # If jq output is already an array
            try:
                items = json.loads(stdout)
                if not isinstance(items, list):
                    items = [items]
            except json.JSONDecodeError:
                logger.warning("Failed to parse gh output on page %d", page)
                break

        if not items:
            break

        all_prs.extend(items)

        if len(items) < per_page:
            break
        page += 1

    return all_prs


def _enrich_pr(raw_pr: dict, owner: str, repo: str) -> dict:
    """Enrich a PR with metadata from the GitHub API."""
    pr_number = raw_pr.get("number", 0)
    title = raw_pr.get("title", "")
    body = raw_pr.get("body")
    author = raw_pr.get("user", {})
    if isinstance(author, dict):
        author = author.get("login", "unknown")
    labels = raw_pr.get("labels", [])

    # Get detailed PR info (merge_sha, changed_files, etc.)
    detail_cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/pulls/{pr_number}",
        "--jq",
        (
            "{merge_commit_sha: .merge_commit_sha, base_sha: .base.sha, "
            "head_sha: .head.sha, changed_files: .changed_files, "
            "additions: .additions, deletions: .deletions, "
            "merged_at: .merged_at, diff_url: .diff_url}"
        ),
    ]

    ok, out, _ = run_cmd_safe(detail_cmd, timeout=30)
    detail = {}
    if ok and out.strip():
        try:
            detail = json.loads(out.strip())
        except json.JSONDecodeError:
            pass

    # Get list of changed files
    files_cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/pulls/{pr_number}/files",
        "--paginate",
        "--jq",
        ".[].filename",
    ]
    ok_files, out_files, _ = run_cmd_safe(files_cmd, timeout=30)
    changed_files = []
    if ok_files and out_files.strip():
        changed_files = [f for f in out_files.strip().split("\n") if f]

    # Classify
    task_type, confidence = _classify_task_type(title, labels)
    subsystem = _detect_subsystem(changed_files)
    impl_files = _extract_implementation_files(changed_files)
    test_files_list = _extract_test_files(changed_files)
    languages = _detect_languages(changed_files)

    # Simple LOC estimation (count changed files)
    impl_loc = detail.get("additions", 0) + detail.get("deletions", 0)
    impl_file_count = len(impl_files)
    test_file_count = len(test_files_list)

    # Check for linked issue
    linked_issue_num, _ = _get_linked_issue(body)

    # Compute complexity
    complexity = _compute_complexity(impl_loc, impl_file_count, 1, 0)

    return {
        "pr_number": pr_number,
        "title": title,
        "body": body,
        "author": author,
        "author_type": "bot" if any(b in author.lower() for b in _BOT_AUTHORS) else "user",
        "labels": labels,
        "merged_at": detail.get("merged_at"),
        "merge_sha": detail.get("merge_commit_sha"),
        "base_sha": detail.get("base_sha"),
        "head_sha": detail.get("head_sha"),
        "changed_files": changed_files,
        "additions": detail.get("additions", 0),
        "deletions": detail.get("deletions", 0),
        "linked_issue_number": linked_issue_num,
        "merge_commit_sha": detail.get("merge_commit_sha"),
        "head_commit_sha": detail.get("head_sha"),
        "diff_url": detail.get("diff_url"),
        "task_type": task_type.value,
        "task_type_confidence": confidence,
        "subsystem": subsystem,
        "complexity": complexity.value,
        "implementation_loc": impl_loc,
        "implementation_files": impl_file_count,
        "test_loc": 0,  # Would need diff-level analysis for accurate test LOC
        "test_files": test_file_count,
        "languages": languages,
        "directories": list(set(str(Path(f).parent) for f in changed_files if "/" in f)),
        "pr_json": json.dumps(raw_pr),
    }


def run_analyze(resync: bool = False, verbose: bool = False) -> None:
    """Analyze repository workload from Git and GitHub history."""
    setup_logging(verbose=verbose)
    cwd = Path.cwd()
    git_root = get_git_root(cwd)
    if git_root is None:
        console.print("[red]Error:[/red] Not inside a Git repository.")
        sys.exit(1)

    # ── Load config ────────────────────────────────────────────────────────
    config = load_config(git_root)
    repobench_dir = git_root / ".repobench"

    if not repobench_dir.exists():
        console.print(
            "[red]Error:[/red] RepoBench not initialized. Run [bold]repobench init[/bold] first."
        )
        sys.exit(1)

    # ── Open database ──────────────────────────────────────────────────────
    db_path = repobench_dir / "state.db"
    db = Database(db_path)
    db.initialize()

    # ── Determine lookback window ──────────────────────────────────────────
    lookback_days = config.repository.lookback_days
    since_date = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    since_str = since_date

    # ── Check for GitHub remote ────────────────────────────────────────────
    owner_repo = get_github_owner_repo(git_root)
    if owner_repo is None:
        console.print("[red]Error:[/red] No GitHub remote detected.")
        db.close()
        sys.exit(1)

    owner, repo = owner_repo
    console.print(f"[cyan]Analyzing:[/cyan] {owner}/{repo} (last {lookback_days} days)")

    # ── Check if gh is authenticated ───────────────────────────────────────
    gh_ok, _, gh_err = run_cmd_safe(["gh", "auth", "status"])
    if not gh_ok:
        console.print("[red]Error:[/red] GitHub CLI not authenticated.")
        console.print(f"[dim]{gh_err}[/dim]")
        db.close()
        sys.exit(1)

    # ── Fetch PRs ──────────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=console,
    ) as progress:
        fetch_task = progress.add_task("Fetching merged PRs...", total=None)

        raw_prs = _fetch_merged_prs(owner, repo, since_str, verbose)
        progress.update(fetch_task, completed=len(raw_prs), total=len(raw_prs))
        progress.update(fetch_task, description=f"Fetched {len(raw_prs)} PRs")

        # ── Enrich and classify ────────────────────────────────────────────
        classify_task = progress.add_task("Classifying PRs...", total=len(raw_prs))

        enriched_count = 0
        skipped_count = 0

        for i, raw_pr in enumerate(raw_prs):
            pr_number = raw_pr.get("number", 0)

            # Skip if already processed (unless resync)
            if not resync:
                existing = db.get_pr(pr_number)
                if existing and existing.get("status") not in (None, "discovered"):
                    progress.advance(classify_task)
                    continue

            # Skip automated maintenance
            if _is_automated_maintenance(raw_pr):
                skipped_count += 1
                progress.advance(classify_task)
                continue

            # Enrich PR
            enriched = _enrich_pr(raw_pr, owner, repo)

            # Skip doc-only
            if _is_doc_only(enriched.get("changed_files", [])):
                skipped_count += 1
                enriched["status"] = "filtered"
                enriched["rejection_reason"] = "DOC_ONLY"

            # Skip oversized
            impl_loc = enriched.get("implementation_loc", 0)
            impl_files = enriched.get("implementation_files", 0)
            mining = config.task_mining
            if (
                impl_loc > mining.max_implementation_loc
                or impl_files > mining.max_implementation_files
            ):
                enriched["status"] = "filtered"
                enriched["rejection_reason"] = "TASK_TOO_LARGE"

            if impl_loc < mining.min_implementation_loc:
                enriched["status"] = "filtered"
                enriched["rejection_reason"] = "TASK_TOO_SMALL"

            # Store in database
            db.upsert_pr(enriched)
            enriched_count += 1

            progress.advance(classify_task)

    # ── Discover candidates ──────────────────────────────────────────────
    console.print("\n[bold]Discovering candidate tasks...[/bold]")
    all_prs_for_candidates = db.get_all_prs()

    # Build PRWorkloadInfo objects for the mining pipeline
    workload_infos: list[PRWorkloadInfo] = []
    for pr_row in all_prs_for_candidates:
        if pr_row.get("status") == "filtered":
            continue
        pr = PullRequest(
            pr_number=pr_row["pr_number"],
            title=pr_row.get("title", ""),
            body=pr_row.get("body"),
            author=pr_row.get("author", ""),
            author_type=pr_row.get("author_type"),
            labels=json.loads(pr_row.get("labels", "[]"))
            if isinstance(pr_row.get("labels"), str)
            else pr_row.get("labels", []),
            merged_at=pr_row.get("merged_at"),
            merge_sha=pr_row.get("merge_sha"),
            base_sha=pr_row.get("base_sha"),
            head_sha=pr_row.get("head_sha"),
            changed_files=json.loads(pr_row.get("changed_files", "[]"))
            if isinstance(pr_row.get("changed_files"), str)
            else pr_row.get("changed_files", []),
            additions=pr_row.get("additions", 0),
            deletions=pr_row.get("deletions", 0),
            linked_issue_number=pr_row.get("linked_issue_number"),
            merge_commit_sha=pr_row.get("merge_commit_sha"),
            head_commit_sha=pr_row.get("head_commit_sha"),
        )
        info = PRWorkloadInfo(
            pr=pr,
            task_type=TaskType(pr_row.get("task_type", "unknown")),
            task_type_confidence=pr_row.get("task_type_confidence", 0.0),
            subsystem=pr_row.get("subsystem", "unknown"),
            complexity=Complexity(pr_row.get("complexity", "medium")),
            implementation_loc=pr_row.get("implementation_loc", 0),
            implementation_files=pr_row.get("implementation_files", 0),
            test_loc=pr_row.get("test_loc", 0),
            test_files=pr_row.get("test_files", 0),
            languages=json.loads(pr_row.get("languages", "[]"))
            if isinstance(pr_row.get("languages"), str)
            else pr_row.get("languages", []),
            directories=json.loads(pr_row.get("directories", "[]"))
            if isinstance(pr_row.get("directories"), str)
            else pr_row.get("directories", []),
        )
        workload_infos.append(info)

    candidates = discover_candidates(workload_infos, config)

    # Store candidates
    for cand in candidates:
        db.upsert_candidate(cand.model_dump())

    candidate_counts = db.count_candidates_by_status()
    discovered = candidate_counts.get("discovered", 0)
    filtered = candidate_counts.get("filtered", 0)
    console.print(f"  Candidates discovered: [cyan]{discovered}[/cyan]")
    console.print(f"  Candidates filtered:   [cyan]{filtered}[/cyan]")

    # ── Print summary ──────────────────────────────────────────────────────
    total_stored = db.count_prs()
    # Compute workload stats from DB
    all_prs = db.get_all_prs()
    type_counts: dict[str, int] = {}
    subsystem_counts: dict[str, int] = {}
    complexity_counts: dict[str, int] = {}

    for pr in all_prs:
        tt = pr.get("task_type", "unknown")
        type_counts[tt] = type_counts.get(tt, 0) + 1
        ss = pr.get("subsystem", "unknown")
        subsystem_counts[ss] = subsystem_counts.get(ss, 0) + 1
        cx = pr.get("complexity", "medium")
        complexity_counts[cx] = complexity_counts.get(cx, 0) + 1

    console.print()

    # Type distribution table
    type_table = Table(title="Task Type Distribution", show_header=True, header_style="bold")
    type_table.add_column("Type", style="cyan")
    type_table.add_column("Count", justify="right")
    type_table.add_column("Percentage", justify="right")

    total_prs = len(all_prs) or 1
    for tt in sorted(type_counts, key=type_counts.get, reverse=True):  # type: ignore[arg-type]
        cnt = type_counts[tt]
        pct = cnt / total_prs * 100
        type_table.add_row(tt, str(cnt), f"{pct:.1f}%")
    console.print(type_table)

    # Subsystem table (top 10)
    if subsystem_counts:
        sub_table = Table(title="Top Subsystems", show_header=True, header_style="bold")
        sub_table.add_column("Subsystem", style="cyan")
        sub_table.add_column("Count", justify="right")
        sub_table.add_column("Percentage", justify="right")

        sorted_subs = sorted(subsystem_counts.items(), key=lambda x: -x[1])[:10]
        for ss, cnt in sorted_subs:
            pct = cnt / total_prs * 100
            sub_table.add_row(ss, str(cnt), f"{pct:.1f}%")
        console.print(sub_table)

    # Complexity table
    cx_table = Table(title="Complexity Distribution", show_header=True, header_style="bold")
    cx_table.add_column("Complexity", style="cyan")
    cx_table.add_column("Count", justify="right")
    cx_table.add_column("Percentage", justify="right")

    for cx in ["small", "medium", "large"]:
        cnt = complexity_counts.get(cx, 0)
        pct = cnt / total_prs * 100
        cx_table.add_row(cx, str(cnt), f"{pct:.1f}%")
    console.print(cx_table)

    # Summary panel
    console.print(
        Panel(
            f"[bold]Repository analyzed successfully[/bold]\n\n"
            f"  Total PRs stored:      [cyan]{total_stored}[/cyan]\n"
            f"  PRs enriched:          [cyan]{enriched_count}[/cyan]\n"
            f"  PRs skipped:           [cyan]{skipped_count}[/cyan]\n"
            f"  Lookback window:       [cyan]{since_str} → now[/cyan]\n"
            f"  Workload Universe:     [cyan]{total_prs}[/cyan] PRs",
            title="[bold green]Analysis Complete[/bold green]",
            border_style="green",
        )
    )

    console.print(
        "\n[bold]Next steps:[/bold]\n"
        "  repobench candidates    # view candidate tasks\n"
        "  repobench benchmark build # build a benchmark\n"
        "  repobench task validate  # validate specific candidates\n"
    )

    db.close()
