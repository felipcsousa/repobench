"""Deterministic task-type classification (PRD §67).

Priority is fixed: 1) labels, 2) conventional-commit prefix, 3) title patterns,
4) diff patterns. No LLM involved — the same PR always yields the same type.
"""

from __future__ import annotations

import re

from repobench.core.testpaths import is_test_path
from repobench.core.types import PRInfo, TaskType

# Label keywords, checked category by category; tokens equal the keyword or
# contain it as a substring when the keyword is 3+ chars ("performance" ~ "perf").
_LABEL_RULES: tuple[tuple[tuple[str, ...], TaskType], ...] = (
    (("bug", "fix"), TaskType.BUGFIX),
    (("feat", "feature"), TaskType.FEATURE),
    (("refactor",), TaskType.REFACTOR),
    (("perf",), TaskType.PERFORMANCE),
    (("dep", "deps", "integration"), TaskType.INTEGRATION),
    (("migrat",), TaskType.MIGRATION),
    (("chore-infra", "infra", "ci", "cd", "docker"), TaskType.INFRASTRUCTURE),
)

# Conventional-commit type prefix, e.g. "feat:", "fix(core):", "chore(deps):".
_CONVENTIONAL_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?\s*:")
_CONVENTIONAL_MAP: dict[str, TaskType] = {
    "feat": TaskType.FEATURE,
    "fix": TaskType.BUGFIX,
    "refactor": TaskType.REFACTOR,
    "perf": TaskType.PERFORMANCE,
    "build": TaskType.INFRASTRUCTURE,
    "ci": TaskType.INFRASTRUCTURE,
    "deps": TaskType.INTEGRATION,
}

# Title keyword patterns, checked in priority order (substring match, lowercase).
_TITLE_RULES: tuple[tuple[tuple[str, ...], TaskType], ...] = (
    (("fix", "bug", "incorrect", "error", "crash"), TaskType.BUGFIX),
    (("add", "support", "implement"), TaskType.FEATURE),
    (("migrate", "upgrade", "port"), TaskType.MIGRATION),
    (("speed", "optim"), TaskType.PERFORMANCE),
    (("refactor", "clean", "rename"), TaskType.REFACTOR),
    (("deps", "bump", "update dependency"), TaskType.INTEGRATION),
)

_INFRA_BASENAMES = {"makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml"}


def _label_tokens(label: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", label.lower()) if token}


def _classify_labels(labels: list[str]) -> TaskType | None:
    for keywords, task_type in _LABEL_RULES:
        for label in labels:
            lowered = label.lower()
            tokens = _label_tokens(label)
            for keyword in keywords:
                if keyword in tokens or (len(keyword) > 2 and keyword in lowered):
                    return task_type
    return None


def _classify_conventional(title: str) -> TaskType | None:
    match = _CONVENTIONAL_RE.match(title.lower())
    if match is None:
        return None
    commit_type, scope = match.group(1), match.group(0)
    if commit_type == "chore":
        # chore(deps) is dependency work; any other chore is infrastructure.
        return TaskType.INTEGRATION if "dep" in scope else TaskType.INFRASTRUCTURE
    return _CONVENTIONAL_MAP.get(commit_type)


def _classify_title(title: str) -> TaskType | None:
    lowered = title.lower()
    for keywords, task_type in _TITLE_RULES:
        if any(keyword in lowered for keyword in keywords):
            return task_type
    return None


def _is_infra_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    basename = lowered.rsplit("/", 1)[-1]
    segments = lowered.split("/")
    return (
        lowered.startswith(".github/")
        or lowered.startswith(".circleci/")
        or ".gitlab-ci" in lowered
        or basename in _INFRA_BASENAMES
        or basename.startswith("dockerfile.")
        or "ci" in segments[:-1]
    )


def _classify_diff(changed_files: list[str]) -> TaskType:
    if not changed_files:
        return TaskType.UNKNOWN
    if all(is_test_path(path) for path in changed_files):
        return TaskType.UNKNOWN  # test-only diffs say nothing about intent
    if all(_is_infra_path(path) for path in changed_files):
        return TaskType.INFRASTRUCTURE
    return TaskType.UNKNOWN


def classify_task_type(pr: PRInfo, changed_files: list[str]) -> TaskType:
    """Classify a merged PR into exactly one TaskType, highest-priority signal wins."""
    by_labels = _classify_labels(pr.labels)
    if by_labels is not None:
        return by_labels
    by_convention = _classify_conventional(pr.title)
    if by_convention is not None:
        return by_convention
    by_title = _classify_title(pr.title)
    if by_title is not None:
        return by_title
    return _classify_diff(changed_files)
