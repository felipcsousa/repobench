"""Repository-relative change scope (PRD §17-21).

Pure, deterministic feature extraction for adaptive candidate mining: no I/O,
no ML, no nondeterminism (PRD §72). These functions replace the absolute
LOC/files hard gate as *evidence*: a diff's size is interpreted against the
repository's own distribution and against how much of it is generated code,
never against a fixed threshold. Callers (assessment, later PRs) combine the
features into scope/confidence scores.
"""

from __future__ import annotations

import posixpath

from pydantic import BaseModel

from repobench.core.testpaths import is_test_path
from repobench.core.types import Complexity

# A POSIX path SEGMENT equal to one of these (case-insensitively) marks the
# file generated (PRD §20): whole directories whose content no human authored
# in this change.
GENERATED_DIR_SEGMENTS = frozenset(
    {
        "generated",
        "__generated__",
        "dist",
        "build",
        "codegen",
        "__snapshots__",
        "snapshots",
        "vendor",
    }
)

# Well-known lockfiles / checksums: one-line semantic edits, thousands of LOC.
GENERATED_BASENAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "cargo.lock",
        "go.sum",
        "composer.lock",
        "gemfile.lock",
    }
)

# Suffix patterns for machine-written artifacts anywhere in the tree.
GENERATED_SUFFIXES = frozenset({".lock", ".snap", ".min.js", ".map"})


def is_generated_path(path: str) -> bool:
    """True when `path` looks machine-written (PRD §20): any POSIX path segment
    in GENERATED_DIR_SEGMENTS, a basename in GENERATED_BASENAMES, or a
    GENERATED_SUFFIXES ending. Matching is case-insensitive on segment and
    basename — build systems capitalize freely (BUILD, Gemfile.lock)."""
    normalized = path.replace("\\", "/").lower()
    segments = normalized.split("/")
    if any(segment in GENERATED_DIR_SEGMENTS for segment in segments):
        return True
    if posixpath.basename(normalized) in GENERATED_BASENAMES:
        return True
    return any(normalized.endswith(suffix) for suffix in GENERATED_SUFFIXES)


# Common implementation-language suffixes -> language label. Unknown suffixes
# are deliberately absent: an unrecognized file is not evidence of a distinct
# language and must not inflate cross-language isolation penalties (PRD §22).
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
}


class ScopeFeatures(BaseModel):
    """Structural diff features of one PR (PRD §17, §21).

    `generated_loc` is a subset of `implementation_loc`; `impl_paths` is kept
    (sorted, for determinism) so callers can reuse the exact split instead of
    re-deriving it.
    """

    implementation_loc: int
    test_loc: int
    generated_loc: int
    implementation_files: int
    test_files: int
    dirs_touched: int
    packages_touched: int
    languages: list[str]
    impl_paths: list[str]


def _first_segment(path: str) -> str:
    # Same notion as repobench/mining/candidates.py: packages are first path
    # segments, with Windows separators normalized away.
    return path.replace("\\", "/").split("/")[0]


def scope_features(changes: list[tuple[int, int, str]]) -> ScopeFeatures:
    """Extract ScopeFeatures from numstat rows (added, removed, path).

    The test/implementation split reuses `is_test_path` — the same boundary as
    repobench/mining/candidates.py, so adaptive features always describe the
    same population as the fields they extend. LOC follows the existing
    convention there: added + removed with negatives floored at 0, so binary
    files (numstat -1 -1) count as files but contribute 0 LOC.
    """
    impl_loc = 0
    test_loc = 0
    generated_loc = 0
    impl_paths: list[str] = []
    test_files = 0
    for added, removed, path in changes:
        loc = max(added, 0) + max(removed, 0)
        if is_test_path(path):
            test_files += 1
            test_loc += loc
            continue
        impl_paths.append(path)
        impl_loc += loc
        if is_generated_path(path):
            generated_loc += loc

    parents = {posixpath.dirname(path.replace("\\", "/")) or "." for path in impl_paths}
    packages = {_first_segment(path) for path in impl_paths if _first_segment(path)}
    languages = {
        LANGUAGE_BY_SUFFIX[posixpath.splitext(path)[1].lower()]
        for path in impl_paths
        if posixpath.splitext(path)[1].lower() in LANGUAGE_BY_SUFFIX
    }
    return ScopeFeatures(
        implementation_loc=impl_loc,
        test_loc=test_loc,
        generated_loc=generated_loc,
        implementation_files=len(impl_paths),
        test_files=test_files,
        dirs_touched=len(parents),
        packages_touched=len(packages),
        languages=sorted(languages),
        impl_paths=sorted(impl_paths),
    )


def behavioral_loc(features: ScopeFeatures) -> int:
    """Generated-discounted LOC (PRD §20): all non-generated implementation
    LOC plus 20% of generated implementation LOC (floored at int, never
    negative — generated_loc is a subset of implementation_loc).

    A 1700-LOC diff that carries 1200 generated LOC must not be planned,
    scored, or described as a 1700-LOC task: lockfile churn and codegen output
    are not what an agent would have to reconstruct. The 20% weight keeps
    generated churn visible (it still signals codegen-heavy changes) without
    letting it dominate the behavioral size.
    """
    return (features.implementation_loc - features.generated_loc) + features.generated_loc // 5


def _rank_nonempty(value: float, values: list[float]) -> float:
    """Rank of `value` within a non-empty `values`: 100 * |{v <= value}| / n."""
    below_or_equal = sum(1 for v in values if v <= value)
    return 100.0 * below_or_equal / len(values)


def percentile_rank(value: float, values: list[float]) -> float | None:
    """Percentile of `value` within `values`, 0-100: 100 * |{v <= value}| / n.

    Empty `values` returns None: with no repository baseline there is nothing
    to rank against, and the honesty rule (PRD §53-54) keeps "not measured"
    distinguishable from a measurement — never an invented neutral P50, P0 or
    P100. Callers decide what no baseline means (e.g. assess_scope scores the
    scope component neutral on None).
    """
    if not values:
        return None
    return _rank_nonempty(value, values)


def complexity_from_percentile(percentile: float) -> Complexity:
    """Repository-relative complexity buckets (PRD §19): small = P0-P35,
    medium = P35-P80, large = P80-P95, xlarge = P95+. Boundary values belong
    to the higher bucket (exactly 35 -> medium, 80 -> large, 95 -> xlarge)."""
    if percentile >= 95:
        return Complexity.XLARGE
    if percentile >= 80:
        return Complexity.LARGE
    if percentile >= 35:
        return Complexity.MEDIUM
    return Complexity.SMALL


def distribution_stats(values: list[float]) -> dict[str, float] | None:
    """P50 / P75 / P90 of `values` for the cap-pressure report (PRD §32).

    Nearest-rank on the sorted list via the same convention as
    `percentile_rank`: each quantile is the smallest observed value whose rank
    reaches it — a value that actually exists in the repository, never an
    interpolation between two samples. Empty input returns None: no baseline
    was measured, and None stays distinguishable from a zeroed measurement.
    """
    if not values:
        return None
    ordered = sorted(values)
    stats: dict[str, float] = {}
    for name, quantile in (("p50", 50.0), ("p75", 75.0), ("p90", 90.0)):
        stats[name] = next(
            v for v in ordered if _rank_nonempty(v, values) >= quantile
        )
    return stats
