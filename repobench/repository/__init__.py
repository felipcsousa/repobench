"""Repository ingestion: git history, GitHub enrichment and workload statistics."""

from __future__ import annotations

from repobench.repository.git import GitRepo, slug_from_url
from repobench.repository.github import GitHubClient
from repobench.repository.workload import (
    build_workload,
    distribution,
    suggest_benchmark_size,
    summarize_analysis,
)

__all__ = [
    "GitHubClient",
    "GitRepo",
    "build_workload",
    "distribution",
    "slug_from_url",
    "suggest_benchmark_size",
    "summarize_analysis",
]
