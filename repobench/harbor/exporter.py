"""Harbor task exporter: generates Harbor-compatible task structure."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from repobench.logging import get_logger
from repobench.models import ValidTask

log = get_logger("harbor.exporter")

# ── Test script template (auto-detect) ────────────────────────────────────────

_TEST_SCRIPT_TEMPLATE = """#!/bin/bash
# RepoBench auto-generated test script
# Detects project language and runs appropriate tests
set -e

TEST_OUTPUT="/tmp/repobench_test_output.txt"
REWARD_DIR="/logs/verifier"
mkdir -p "$REWARD_DIR"

# Detect project type and run tests
if [ -f "go.mod" ]; then
    echo "Detected: Go project"
    go test ./... 2>&1 | tee "$TEST_OUTPUT"
elif [ -f "pom.xml" ]; then
    echo "Detected: Maven/Java project"
    mvn test -q 2>&1 | tee "$TEST_OUTPUT"
elif [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
    echo "Detected: Gradle/Java project"
    if [ -f "./gradlew" ]; then
        ./gradlew test --quiet 2>&1 | tee "$TEST_OUTPUT"
    else
        gradle test --quiet 2>&1 | tee "$TEST_OUTPUT"
    fi
elif [ -f "package.json" ]; then
    echo "Detected: Node.js project"
    npm test 2>&1 | tee "$TEST_OUTPUT"
elif [ -f "pyproject.toml" ] || [ -f "pytest.ini" ] || [ -f "setup.cfg" ]; then
    echo "Detected: Python project"
    python -m pytest 2>&1 | tee "$TEST_OUTPUT"
elif [ -f "requirements.txt" ]; then
    echo "Detected: Python project (pip)"
    python -m pytest 2>&1 | tee "$TEST_OUTPUT"
else
    echo "No test framework detected"
    echo "No test framework detected" > "$TEST_OUTPUT"
    echo 0 > "$REWARD_DIR/reward.txt"
    exit 0
fi

# Write reward based on exit code
if [ $? -eq 0 ]; then
    echo "Tests PASSED"
    echo 1 > "$REWARD_DIR/reward.txt"
else
    echo "Tests FAILED"
    echo 0 > "$REWARD_DIR/reward.txt"
fi
"""


def export_harbor_tasks(
    benchmark_id: str,
    tasks: list[ValidTask],
    output_dir: Path,
    network_mode: str = "no-network",
) -> Path:
    """Export tasks to a Harbor-compatible directory structure.

    Layout:
        output_dir/
          task-001/
            instruction.md
            task.toml
            tests/
              test.sh
            environment/
          task-002/
            ...

    Returns the output directory path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, task in enumerate(tasks, start=1):
        task_dir = output_dir / f"task-{idx:03d}"
        export_single_task(benchmark_id, task, idx, task_dir, network_mode)

    # ── manifest.json ──────────────────────────────────────────────────────
    manifest = {
        "benchmark_id": benchmark_id,
        "tasks": [
            {
                "index": idx,
                "task_id": task.task_id,
                "candidate_id": task.candidate.candidate_id,
                "pr_number": task.candidate.pr_number,
            }
            for idx, task in enumerate(tasks, start=1)
        ],
        "task_count": len(tasks),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log.info("Exported %d Harbor tasks to %s", len(tasks), output_dir)
    return output_dir


def export_single_task(
    benchmark_id: str,
    task: ValidTask,
    index: int,
    task_dir: Path,
    network_mode: str = "no-network",
) -> Path:
    """Export a single task to Harbor format.

    Creates task.toml, instruction.md, tests/test.sh, and environment/.
    Returns the task directory path.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    c = task.candidate

    # ── instruction.md ─────────────────────────────────────────────────────
    instruction = task.instruction_text or f"Fix the issue described in PR #{c.pr_number}."
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # ── task.toml (Harbor schema 1.4) ──────────────────────────────────────
    task_toml = _build_task_toml(benchmark_id, task, index, network_mode)
    (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")

    # ── tests/test.sh ──────────────────────────────────────────────────────
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    test_script = tests_dir / "test.sh"
    test_script.write_text(_TEST_SCRIPT_TEMPLATE, encoding="utf-8")
    # Make executable
    test_script.chmod(test_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # ── Copy verifier files into tests/ ────────────────────────────────────
    for vf in task.verifier_files:
        vf_name = Path(vf).name
        # Write a placeholder noting the original path
        (tests_dir / f".{vf_name}.origin").write_text(f"Original path: {vf}\n", encoding="utf-8")

    # ── environment/ ───────────────────────────────────────────────────────
    env_dir = task_dir / "environment"
    env_dir.mkdir(exist_ok=True)
    (env_dir / "README.md").write_text(
        "# Environment\n\nProvided by RepoBench environment builder.\n"
        "The test script auto-detects the project language.\n",
        encoding="utf-8",
    )

    log.debug("Exported task %s -> %s", task.task_id, task_dir)
    return task_dir


def _build_task_toml(
    benchmark_id: str,
    task: ValidTask,
    index: int,
    network_mode: str = "no-network",
) -> str:
    """Build a Harbor task.toml file (schema_version 1.4)."""
    c = task.candidate
    task_name = f"repobench/{benchmark_id}/{task.task_id}"
    description = (c.pr_title or f"PR #{c.pr_number}")[:200]

    return f'''schema_version = "1.4"

[task]
name = "{task_name}"
version = "1.0.0"
description = {json.dumps(description)}
authors = [{{ name = "RepoBench", email = "repobench@local" }}]
keywords = ["coding-agent", "eval", "{c.task_type.value if c.task_type else "unknown"}"]

[metadata]
pr_number = {c.pr_number}
candidate_id = "{c.candidate_id}"
subsystem = "{c.subsystem}"
complexity = "{c.complexity.value if c.complexity else "unknown"}"
task_type = "{c.task_type.value if c.task_type else "unknown"}"
benchmark_id = "{benchmark_id}"
repobench_version = "0.1.0"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
network_mode = "{network_mode}"
build_timeout_sec = 600.0
'''
