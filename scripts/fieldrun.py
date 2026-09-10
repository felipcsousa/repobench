"""Field Validation Wave runner (PRD v0.9 §47-52, Epic 6).

Orchestrates the 30-repository matrix protocol with zero unit tests by design:
it shells the real network/git/gh world the PRD demands (clone → init →
analyze → optional build), which the hermetic suite must never touch.

Protocol per repository (PRD §50-52):
  1. clone (local paths pass through unchanged)
  2. repobench init            — zero config tuning
  3. analyze (hard_caps)       — pass 1, the old default
  4. rewrite size_policy=adaptive → analyze — pass 2, the ≤1-config-change rule
  5. optional --build: benchmark build under adaptive (priority order, budget)
  6. per-repo result JSON + aggregate summary.json in --out

Collected per repository (PRD §51): merged PRs, funnel stages, candidate
classes, VALID tasks, rejection codes, benchmark size/health, elapsed time and
the exact config interventions performed. Aggregate summary reports the
old-vs-new comparison (PRD §52: candidate recall, zero-result rate).

Usage:
  uv run scripts/fieldrun.py --repos matrix.txt --out field-results [--build]
matrix.txt: one repository URL or local path per line; '#' comments allowed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from repobench.cli.builds import build_benchmark
from repobench.cli.services import (
    analyze_repository,
    init_project,
    persist_candidates,
)
from repobench.config import RepoBenchConfig
from repobench.core.errors import RepoBenchError
from repobench.storage.db import Storage


def slug_for(source: str) -> str:
    """Stable directory name for a repo URL or local path."""
    cleaned = source.rstrip("/").removesuffix(".git")
    return cleaned.replace("/", "_").replace(":", "_").replace("@", "")


def clone_or_local(source: str, workdir: Path) -> Path:
    """Local path → itself; URL → shallow-full clone into workdir (full history:
    mining needs the merged-PR window, not just HEAD)."""
    if not source.startswith(("http://", "https://", "git@", "ssh://")):
        return Path(source).resolve()
    target = workdir / slug_for(source)
    if not target.exists():
        subprocess.run(
            ["git", "clone", source, str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    return target


def _analyze_snapshot(root: Path, cfg: RepoBenchConfig) -> dict:
    outcome = analyze_repository(root, cfg)
    persist_candidates(Storage(root / ".repobench" / "state.db"), outcome.candidates)
    funnel = outcome.funnel
    snapshot = {
        "merged_prs": outcome.merged_prs,
        "human_changes": funnel.human_changes,
        "with_verifier_evidence": funnel.with_verifier_evidence,
        "with_instruction": funnel.with_instruction,
        "pool": funnel.discovered + funnel.low_priority,
        "classes": {
            "high": funnel.high_confidence,
            "medium": funnel.medium_confidence,
            "experimental": funnel.experimental,
        },
        "benchmarkability": {
            "score": outcome.benchmarkability.score,
            "band": outcome.benchmarkability.band,
        },
        "scope_profile": outcome.scope_profile.model_dump(),
        "rejections": {},
    }
    for candidate in outcome.candidates:
        if candidate.rejection_code is not None:
            key = candidate.rejection_code.value
            snapshot["rejections"][key] = snapshot["rejections"].get(key, 0) + 1
    return snapshot


def run_one(
    source: str, workdir: Path, outdir: Path, *, build: bool, build_size: int
) -> dict:
    """The full per-repo protocol; failures are recorded, never fatal to the
    matrix (PRD §47 needs the failures as data too)."""
    started = time.monotonic()
    result: dict = {"source": source, "interventions": []}
    try:
        root = clone_or_local(source, workdir)
        result["repo_dir"] = str(root)
        init = init_project(root, overwrite=False)
        result["interventions"].append(
            "init: environment detection (zero tuning)" if init.created else "init: config existed"
        )
        cfg_path = root / "repobench.yml"
        cfg = RepoBenchConfig.load(cfg_path)

        # Pass 1 — the legacy default, untouched (PRD §52 old-vs-new baseline).
        result["hard_caps"] = _analyze_snapshot(root, cfg)

        # Pass 2 — the one allowed config modification (PRD §50).
        cfg.task_mining.size_policy = "adaptive"
        cfg.save(cfg_path)
        result["interventions"].append("config: task_mining.size_policy=adaptive")
        result["adaptive"] = _analyze_snapshot(root, cfg)

        if build:
            storage = Storage(root / ".repobench" / "state.db")
            lines: list[str] = []
            outcome = build_benchmark(
                root, cfg, storage, size=build_size, log=lines.append
            )
            result["build"] = {
                "benchmark_id": outcome.benchmark_id,
                "requested": outcome.requested_size,
                "valid": len(outcome.valid),
                "sample": len(outcome.sample),
                "health": outcome.health.overall,
                "insufficient": outcome.insufficient,
                "budget_stopped_at": outcome.budget_stopped_at,
                "rejected": {
                    code: sum(
                        1
                        for r in outcome.rejected
                        if r.rejection_code and r.rejection_code.value == code
                    )
                    for code in sorted(
                        {
                            r.rejection_code.value
                            for r in outcome.rejected
                            if r.rejection_code
                        }
                    )
                },
            }
        result["status"] = "ok"
    except (RepoBenchError, subprocess.CalledProcessError, OSError) as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    (outdir / f"{slug_for(source)}.json").write_text(json.dumps(result, indent=2))
    return result


def aggregate(results: list[dict]) -> dict:
    """PRD §52 comparison + §57 activation metrics, from real snapshots only —
    repositories without both passes are excluded from the ratios."""
    both = [r for r in results if "hard_caps" in r and "adaptive" in r]
    zero = {
        "hard_caps": sum(1 for r in both if r["hard_caps"]["pool"] == 0),
        "adaptive": sum(1 for r in both if r["adaptive"]["pool"] == 0),
    }
    recall_gain = [
        {
            "repo": r["source"],
            "old": r["hard_caps"]["pool"],
            "new": r["adaptive"]["pool"],
        }
        for r in both
        if r["adaptive"]["pool"] != r["hard_caps"]["pool"]
    ]
    benchmarkable = [
        {
            "repo": r["source"],
            "valid_tasks": r["build"]["valid"],
            "benchmarkable_10": r["build"]["valid"] >= 10,
        }
        for r in results
        if "build" in r
    ]
    return {
        "repos_total": len(results),
        "repos_ok": sum(1 for r in results if r["status"] == "ok"),
        "repos_failed": sum(1 for r in results if r["status"] == "failed"),
        "analyzed_both_policies": len(both),
        "zero_result_rate": zero,
        "candidate_recall_gain": recall_gain,
        "benchmarkability_after_adaptive": benchmarkable,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repos", type=Path, required=True, help="file with one repo URL/path per line")
    parser.add_argument("--out", type=Path, default=Path("field-results"))
    parser.add_argument("--workdir", type=Path, default=Path("field-work"))
    parser.add_argument("--build", action="store_true", help="also run benchmark build (slow)")
    parser.add_argument("--build-size", type=int, default=24)
    args = parser.parse_args(argv)

    if shutil.which("git") is None:
        print("error: git is required", file=sys.stderr)
        return 1

    sources = [
        line.strip()
        for line in args.repos.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not sources:
        print("error: no repositories listed", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    args.workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, source in enumerate(sources, start=1):
        print(f"[{i}/{len(sources)}] {source}")
        result = run_one(
            source, args.workdir, args.out, build=args.build, build_size=args.build_size
        )
        results.append(result)
        print(f"    {result['status']} in {result['elapsed_seconds']}s")

    summary = aggregate(results)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
