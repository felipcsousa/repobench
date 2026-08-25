"""Workload characterization: task type, subsystem, complexity."""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath

from repobench.logging import get_logger
from repobench.models import Complexity, PRWorkloadInfo, PullRequest, TaskType

log = get_logger("repository.workload")


def classify_task_type(pr: PullRequest) -> tuple[TaskType, float]:
    """Classify the task type of a PR using labels, title, and conventions.

    Returns (task_type, confidence).
    """
    scores: dict[TaskType, float] = {
        TaskType.BUGFIX: 0.0,
        TaskType.FEATURE: 0.0,
        TaskType.REFACTOR: 0.0,
    }

    # --- 1. GitHub labels (highest priority) ---
    label_map = {
        TaskType.BUGFIX: ["bug", "bugfix", "regression", "fix", "hotfix"],
        TaskType.FEATURE: ["feature", "enhancement", "feat", "improvement"],
        TaskType.REFACTOR: ["refactor", "cleanup", "chore", "tech-debt", "technical-debt"],
    }
    pr_labels_lower = [l.lower() for l in pr.labels]
    for task_type, keywords in label_map.items():
        for kw in keywords:
            if any(kw in label for label in pr_labels_lower):
                scores[task_type] = max(scores[task_type], 0.95)

    # --- 2. Conventional commit / PR title patterns ---
    title = (pr.title or "").lower()

    bugfix_patterns = [
        r"\bfix\b",
        r"\bbug\b",
        r"\bregression\b",
        r"\bbroken\b",
        r"\bincorrect\b",
        r"\berror\b",
        r"\bcrash\b",
        r"\bpanic\b",
        r"\bsegfault\b",
        r"\bnull\b.*\bpointer\b",
        r"\bexception\b",
    ]
    feature_patterns = [
        r"\bfeat\b",
        r"\bfeature\b",
        r"\badd\b",
        r"\bimplement\b",
        r"\bsupport\b",
        r"\bnew\b",
        r"\benhance\b",
        r"\bextend\b",
    ]
    refactor_patterns = [
        r"\brefactor\b",
        r"\bcleanup\b",
        r"\breorganiz\b",
        r"\bsimplif\b",
        r"\bextract\b",
        r"\brenam\b",
        r"\bmove\b",
    ]

    for pattern in bugfix_patterns:
        if re.search(pattern, title):
            scores[TaskType.BUGFIX] = max(scores[TaskType.BUGFIX], 0.7)

    for pattern in feature_patterns:
        if re.search(pattern, title):
            scores[TaskType.FEATURE] = max(scores[TaskType.FEATURE], 0.7)

    for pattern in refactor_patterns:
        if re.search(pattern, title):
            scores[TaskType.REFACTOR] = max(scores[TaskType.REFACTOR], 0.6)

    # --- 3. Diff characteristics as secondary signal ---
    if pr.changed_files:
        test_file_ratio = _test_file_ratio(pr.changed_files)
        source_only_ratio = 1.0 - test_file_ratio

        # PRs that change many test files relative to source might be test fixes
        if test_file_ratio > 0.7:
            scores[TaskType.BUGFIX] = max(scores[TaskType.BUGFIX], 0.4)

    # --- Determine winner ---
    if not any(v > 0 for v in scores.values()):
        return TaskType.UNKNOWN, 0.0

    best_type = max(scores, key=lambda t: scores[t])
    confidence = scores[best_type]

    # If no label match and only title match, reduce confidence slightly
    if confidence > 0 and confidence < 0.8 and not pr.labels:
        confidence *= 0.9

    return best_type, round(confidence, 2)


def detect_subsystem(
    changed_files: list[str], codeowners: dict[str, list[str]] | None = None
) -> str:
    """Detect the subsystem from changed files and optional CODEOWNERS.

    Priority:
    A. CODEOWNERS pattern match
    B. Workspace/package directory
    C. Stable directory (first or second significant path component)
    D. "unknown"
    """
    if not changed_files:
        return "unknown"

    # --- A. CODEOWNERS ---
    if codeowners:
        for pattern, owners in codeowners.items():
            for f in changed_files:
                if _matches_glob(pattern, f):
                    return pattern.strip("*").strip("/").split("/")[0] or "unknown"

    # --- B. Workspace/package directory ---
    workspace_patterns = ["packages/", "apps/", "services/", "libs/", "modules/"]
    for f in changed_files:
        for wp in workspace_patterns:
            if f.startswith(wp):
                parts = f.split("/")
                if len(parts) >= 2:
                    return parts[1]

    # --- C. Stable directory ---
    dir_counts: dict[str, int] = {}
    for f in changed_files:
        parts = PurePosixPath(f).parts
        if len(parts) >= 2:
            # Skip common prefix directories
            skip = {"src", "lib", "app", "pkg", "internal"}
            for part in parts:
                if part not in skip:
                    dir_counts[part] = dir_counts.get(part, 0) + 1
                    break

    if dir_counts:
        return max(dir_counts, key=lambda k: dir_counts[k])

    return "unknown"


def calculate_complexity(
    impl_loc: int,
    impl_files: int,
    pkg_touched: int,
    test_loc: int,
) -> Complexity:
    """Calculate relative complexity within a repository.

    Uses a log-weighted heuristic as described in the PRD:
        complexity_raw = 0.45 * log(impl_loc)
                       + 0.30 * log(impl_files)
                       + 0.15 * packages_touched
                       + 0.10 * log(test_loc + 1)

    Buckets: Small (P0-P35), Medium (P35-P85), Large (P85-P100).
    Since we don't have the full population distribution here,
    we use absolute thresholds that approximate the percentiles.
    """
    raw = (
        0.45 * math.log(max(impl_loc, 1))
        + 0.30 * math.log(max(impl_files, 1))
        + 0.15 * pkg_touched
        + 0.10 * math.log(max(test_loc, 0) + 1)
    )

    # Calibrated thresholds: Small < ~4.0, Large > ~4.5
    # A 20-LOC single-file change scores ~1.6; a 150-LOC/4-file change
    # scores ~3.4; a 500-LOC/10-file multi-package change scores ~4.8.
    # Large changes exceed the V1 mining bounds (max 400 LOC) by design.
    if raw < 4.0:
        return Complexity.SMALL
    elif raw < 4.5:
        return Complexity.MEDIUM
    else:
        return Complexity.LARGE


def build_workload_info(
    pr: PullRequest,
    codeowners: dict[str, list[str]] | None = None,
) -> PRWorkloadInfo:
    """Build enriched workload info for a single PR."""
    task_type, confidence = classify_task_type(pr)
    subsystem = detect_subsystem(pr.changed_files, codeowners)

    # Count test files
    test_files = [f for f in pr.changed_files if _is_test_file(f)]
    impl_files_list = [f for f in pr.changed_files if not _is_test_file(f)]

    # Estimate LOC from additions/deletions (simplified)
    test_loc = _estimate_test_loc(pr, test_files)
    impl_loc = max(0, pr.additions + pr.deletions - test_loc)

    # Detect languages from file extensions
    languages = _detect_languages_from_files(pr.changed_files)

    # Detect directories
    directories = _extract_directories(pr.changed_files)

    complexity = calculate_complexity(
        impl_loc=impl_loc,
        impl_files=len(impl_files_list),
        pkg_touched=len(set(directories)),
        test_loc=test_loc,
    )

    return PRWorkloadInfo(
        pr=pr,
        task_type=task_type,
        task_type_confidence=confidence,
        subsystem=subsystem,
        complexity=complexity,
        implementation_loc=impl_loc,
        implementation_files=len(impl_files_list),
        test_loc=test_loc,
        test_files=len(test_files),
        languages=languages,
        directories=directories,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _is_test_file(filepath: str) -> bool:
    """Check if a file is a test file."""
    name = PurePosixPath(filepath).name.lower()
    parts = PurePosixPath(filepath).parts

    # Python
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if "tests" in parts or "test" in parts:
        if name.endswith(".py"):
            return True

    # JS/TS
    if name.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx")):
        return True
    if name.endswith((".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")):
        return True
    if "__tests__" in parts:
        return True

    # Go
    if name.endswith("_test.go"):
        return True
    if "testdata" in parts:
        return True

    # Java (name is lowercased, so match lowercase patterns)
    if name.endswith("test.java") or name.endswith("tests.java"):
        return True
    if name.endswith("it.java"):  # Maven integration tests
        return True
    if "test" in parts and name.endswith(".java"):
        # Standard Maven/Gradle layout: src/test/java/...
        return True

    # Snapshot files
    if "__snapshots__" in parts:
        return True

    # Test fixtures
    if "fixtures" in parts and any(kw in name for kw in ["test", "spec", "fixture"]):
        return True

    return False


def _test_file_ratio(files: list[str]) -> float:
    """Calculate the ratio of test files to total files."""
    if not files:
        return 0.0
    test_count = sum(1 for f in files if _is_test_file(f))
    return test_count / len(files)


def _estimate_test_loc(pr: PullRequest, test_files: list[str]) -> int:
    """Estimate test LOC from changed files.

    Simple heuristic: distribute additions proportionally based on
    the ratio of test files to total files.
    """
    if not pr.changed_files or not test_files:
        return 0

    total = len(pr.changed_files)
    test_ratio = len(test_files) / total
    return int((pr.additions + pr.deletions) * test_ratio)


def _detect_languages_from_files(files: list[str]) -> list[str]:
    """Detect languages from file extensions."""
    ext_lang: dict[str, str] = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
    }
    langs = set()
    for f in files:
        ext = PurePosixPath(f).suffix.lower()
        if ext in ext_lang:
            langs.add(ext_lang[ext])
    return sorted(langs)


def _extract_directories(files: list[str]) -> list[str]:
    """Extract unique top-level directories from file paths."""
    dirs = set()
    for f in files:
        parts = PurePosixPath(f).parts
        if len(parts) >= 2:
            dirs.add(parts[0])
    return sorted(dirs)


def _matches_glob(pattern: str, filepath: str) -> bool:
    """Simple glob matching for CODEOWNERS patterns."""
    import fnmatch

    # Normalize: CODEOWNERS patterns may start with /
    pattern = pattern.lstrip("/")
    # Try direct match
    if fnmatch.fnmatch(filepath, pattern):
        return True
    # Try with ** expansion
    if "**" in pattern:
        # Simple: check if the pattern without ** matches a prefix
        prefix = pattern.split("**")[0].rstrip("/")
        if prefix and filepath.startswith(prefix):
            return True
    return False
