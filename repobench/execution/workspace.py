"""Trial workspace lifecycle (PRD §32-35, §59-62).

Workspaces are temporary directories materialized from `git archive BASE` with a fresh
synthetic Git repository — never `git worktree`, which stays connected to the original
history and would leak the solution.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from repobench.core.testpaths import is_test_path
from repobench.execution.process import run_sync

SYNTHETIC_BASE_COMMIT_MESSAGE = "RepoBench benchmark base"
_GIT_IDENTITY = ("-c", "user.name=RepoBench", "-c", "user.email=repobench@localhost")


class Workspace:
    def __init__(self, trial_id: str, task_id: str, repo_dir: Path, base_dir: Path):
        self.trial_id = trial_id
        self.task_id = task_id
        self.repo_dir = repo_dir  # cwd handed to the harness; contains the synthetic git repo
        self.base_dir = base_dir  # trial directory (parent of repo_dir)


def _git(repo_dir: Path, *args: str, timeout: int = 300):
    return run_sync(["git", *_GIT_IDENTITY, *args], cwd=repo_dir, timeout_seconds=timeout)


def _init_synthetic_git(repo_dir: Path) -> None:
    r = _git(repo_dir, "init", "--quiet", "--initial-branch=main")
    if r.exit_code != 0:
        raise RuntimeError(f"git init failed in {repo_dir}: {r.stderr.strip()}")
    _git(repo_dir, "add", "-A")
    r = _git(
        repo_dir,
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        SYNTHETIC_BASE_COMMIT_MESSAGE,
    )
    if r.exit_code != 0:
        raise RuntimeError(f"synthetic base commit failed in {repo_dir}: {r.stderr.strip()}")


class WorkspaceManager:
    """Creates and destroys per-trial workspaces under .repobench/workspaces/."""

    def __init__(self, workspaces_dir: Path, keep: bool = False):
        self.workspaces_dir = Path(workspaces_dir)
        self.keep = keep

    def create(self, trial_id: str, task_id: str, base_archive: Path) -> Workspace:
        trial_dir = self.workspaces_dir / trial_id
        repo_dir = trial_dir / "repo"
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        repo_dir.mkdir(parents=True)
        with tarfile.open(base_archive) as tar:
            tar.extractall(repo_dir, filter="data")
        _init_synthetic_git(repo_dir)
        return Workspace(trial_id=trial_id, task_id=task_id, repo_dir=repo_dir, base_dir=trial_dir)

    def destroy(self, ws: Workspace) -> None:
        if self.keep:
            return
        shutil.rmtree(ws.base_dir, ignore_errors=True)


def verify_synthetic_invariants(repo_dir: Path) -> list[str]:
    """PRD §35: single synthetic commit, no remotes, no original branches."""
    violations: list[str] = []
    r = _git(repo_dir, "log", "--format=%s")
    subjects = [line for line in r.stdout.splitlines() if line.strip()]
    if subjects != [SYNTHETIC_BASE_COMMIT_MESSAGE]:
        violations.append(f"unexpected git log subjects: {subjects!r}")
    r = _git(repo_dir, "remote")
    if r.stdout.strip():
        violations.append(f"unexpected git remotes: {r.stdout.strip()!r}")
    r = _git(repo_dir, "branch", "-a")
    branches = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if len(branches) > 1:
        violations.append(f"unexpected branches: {branches!r}")
    return violations


def diff_stats(diff_text: str) -> tuple[int, int, int]:
    """Returns (files_changed, loc_added, loc_removed) from a unified diff."""
    files, added, removed = 0, 0, 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return files, added, removed


def tampered_test_paths(diff_text: str) -> list[str]:
    """Repo-relative test paths a unified diff touches (issue #18).

    Parses the exact text capture_agent_patch writes to agent.patch. Every path
    mentioned in a `diff --git`, `---`/`+++`, rename or copy header counts — a
    test file the agent deletes or renames is tampering just as much as one it
    edits, so the +++ line alone is not enough. Returns sorted, de-duplicated
    paths; an empty or unparseable diff yields [] — detection degrades to off,
    it never false-accuses.
    """
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            # "diff --git a/<old> b/<new>" — split on the " b/" join so both
            # sides count: a pure rename carries no ---/+++ lines at all.
            rest = line[len("diff --git "):]
            if rest.startswith("a/") and " b/" in rest:
                old, new = rest[2:].split(" b/", 1)
                paths.update((old, new))
        elif line.startswith("--- ") or line.startswith("+++ "):
            target = line[4:]
            if target.startswith(("a/", "b/")):
                paths.add(target[2:])
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            paths.add(line.split(" ", 2)[2])
    return sorted({p for p in paths if p and p != "/dev/null" and is_test_path(p)})


def capture_agent_patch(repo_dir: Path, out_file: Path) -> tuple[int, int, int, list[str]]:
    """Capture the final working tree vs the synthetic BASE, agnostic to commits the
    agent may have created (PRD §60-61). Returns diff stats plus the tampered test
    paths in the agent's final diff (issue #18) — a reward-hacking signal, never
    part of the verdict (PRD §42: verifiers define correctness)."""
    _git(repo_dir, "add", "-A")
    r = _git(repo_dir, "rev-list", "--max-parents=0", "HEAD")
    root = r.stdout.strip().splitlines()[0]
    diff = _git(repo_dir, "diff", "--cached", root)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(diff.stdout)
    files, added, removed = diff_stats(diff.stdout)
    return files, added, removed, tampered_test_paths(diff.stdout)


def snapshot_tree(source_repo_dir: Path, dest_dir: Path) -> Path:
    """Copy the final agent tree (minus .git) into a fresh verification workspace with
    its own synthetic git repo, so verifier runs never mutate the agent's workspace (PRD §62)."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(source_repo_dir, dest_dir, ignore=shutil.ignore_patterns(".git"))
    _init_synthetic_git(dest_dir)
    return dest_dir


def apply_git_patch(directory: Path, patch_file: Path) -> tuple[bool, str]:
    r = _git(directory, "apply", "--whitespace=nowarn", str(patch_file))
    return r.exit_code == 0, r.stderr
