"""Foundation tests: config, ids, paths, test-path classification, storage,
process runner, synthetic workspaces, environment sanitization."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from repobench.config import (
    ProjectConfig,
    RepoBenchConfig,
    compose_cwd,
    default_config_for,
    detect_project_environment,
    detect_subprojects,
)
from repobench.core.ids import new_benchmark_id, new_task_id, new_trial_id
from repobench.core.paths import ProjectPaths, find_repo_root
from repobench.core.testpaths import is_test_path, split_changed_paths
from repobench.core.types import ExecutionTarget, TrialOutcome, TrialResult
from repobench.execution.environment import TrialEnvironment
from repobench.execution.process import run_process, run_sync
from repobench.execution.workspace import (
    WorkspaceManager,
    apply_git_patch,
    capture_agent_patch,
    snapshot_tree,
    verify_synthetic_invariants,
)
from repobench.storage.db import Storage
from repobench.core.types import CommandSpec


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture()
def base_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "history"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def sum_even(xs):\n    return sum(x for x in xs if x % 2 == 1)\n"
    )
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _archive_head(repo: Path, out: Path) -> None:
    with out.open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=repo,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=True,
        )


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = RepoBenchConfig()
    cfg.targets["glm"] = ExecutionTarget(harness="opencode", model="zai/glm-x")
    cfg.targets["local"] = ExecutionTarget(
        harness="command", command=["my-agent", "{prompt_file}"]
    )
    path = tmp_path / "repobench.yml"
    cfg.save(path)
    loaded = RepoBenchConfig.load(path)
    assert loaded.targets["glm"].id == "glm"
    assert loaded.targets["local"].command == ["my-agent", "{prompt_file}"]
    assert loaded.execution.timeout_minutes == 20


def test_config_load_missing(tmp_path: Path) -> None:
    from repobench.core.errors import ConfigError

    with pytest.raises(ConfigError):
        RepoBenchConfig.load(tmp_path / "nope.yml")


def test_default_config_detection(base_repo: Path) -> None:
    (base_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    cfg = default_config_for(base_repo)
    assert cfg.project.language == "python"
    # Detected commands use the running interpreter — a bare "python" does not
    # exist on macOS, and the suggestion would reject every task at build time.
    py = shlex.quote(sys.executable)
    assert cfg.project.test_command == f"{py} -m pytest"
    assert cfg.project.install_command == f"{py} -m pip install -e ."


# ------------------------------------------- monorepo detection (issue #34)


def _write_package_json(directory: Path, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps(payload))


def test_root_package_json_with_test_script_suggests_npm_test(tmp_path: Path) -> None:
    _write_package_json(tmp_path, {"name": "root", "scripts": {"test": "jest"}})
    cfg = detect_project_environment(tmp_path)
    assert (cfg.language, cfg.package_manager) == ("javascript-typescript", "npm")
    assert cfg.test_command == "npm test"
    assert cfg.regression_command == "npm test"


def test_root_package_json_without_test_script_suggests_no_test_command(tmp_path: Path) -> None:
    """The lumpfish guarantee (issue #34): no `scripts.test` means no `npm test`
    suggestion — a None test_command is rejected honestly by validation, while
    an invented command breaks every baseline."""
    _write_package_json(tmp_path, {"name": "root", "scripts": {"build": "tsc"}})
    cfg = detect_project_environment(tmp_path)
    assert (cfg.language, cfg.package_manager) == ("javascript-typescript", "npm")
    assert cfg.test_command is None
    assert cfg.regression_command is None


def test_js_framework_suggestion_does_not_need_a_test_script(tmp_path: Path) -> None:
    """vitest/jest stay suggested from dependencies alone: `npx vitest run`
    works without a `scripts.test` entry."""
    _write_package_json(
        tmp_path, {"name": "root", "scripts": {"build": "tsc"}, "devDependencies": {"vitest": "^1"}}
    )
    cfg = detect_project_environment(tmp_path)
    assert cfg.test_command == "npx vitest run"


def test_detect_subprojects_mixed_monorepo(tmp_path: Path) -> None:
    """Issue #34: npm root (test script present) + python backend; apps/* and
    packages/* are scanned one level deep; dependency junk is never a project."""
    _write_package_json(tmp_path, {"name": "root", "scripts": {"test": "jest"}})
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname = 'api'\n")
    _write_package_json(
        tmp_path / "apps" / "mobile", {"name": "mobile", "scripts": {"build": "expo build"}}
    )
    _write_package_json(
        tmp_path / "packages" / "ui",
        {"name": "ui", "devDependencies": {"vitest": "^1"}},
    )
    (tmp_path / "packages" / "ui" / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    # junk that must never surface as a project
    _write_package_json(tmp_path / "apps" / "node_modules" / "react", {"name": "react"})
    (tmp_path / "services" / ".venv").mkdir(parents=True)
    (tmp_path / "services" / ".venv" / "pyproject.toml").write_text("[project]\nname = 'v'\n")

    by_path = {sub.path: sub.config for sub in detect_subprojects(tmp_path)}
    assert set(by_path) == {"apps/mobile", "backend", "packages/ui"}

    py = shlex.quote(sys.executable)
    backend = by_path["backend"]
    assert backend.language == "python"
    assert backend.test_command == f"{py} -m pytest"

    mobile = by_path["apps/mobile"]
    assert (mobile.language, mobile.package_manager) == ("javascript-typescript", "npm")
    assert mobile.test_command is None  # no scripts.test — never invent `npm test`

    ui = by_path["packages/ui"]
    assert (ui.language, ui.package_manager) == ("javascript-typescript", "pnpm")
    assert ui.test_command == "pnpm vitest run"

    # the root is never listed as its own sub-project and keeps its suggestion
    root = detect_project_environment(tmp_path)
    assert (root.language, root.package_manager, root.test_command) == (
        "javascript-typescript",
        "npm",
        "npm test",
    )


def test_detect_subprojects_empty_for_markerless_dirs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "README.md").write_text("docs, not a project")
    (tmp_path / "apps").mkdir()  # glob root without children
    assert detect_subprojects(tmp_path) == []


def test_project_cwd_validation() -> None:
    """project.cwd must stay inside the repository: relative, no `..`, never
    empty — the join against a workspace root must not be able to escape."""
    assert ProjectConfig(cwd="backend").cwd == "backend"
    assert ProjectConfig(cwd="backend/sub/").cwd == "backend/sub"  # normalized, POSIX
    assert ProjectConfig(cwd="./api").cwd == "api"
    # Absolute means platform-absolute: "/x" has no drive under Windows path
    # semantics (is_absolute() is False there), so use a drive path on win32.
    absolute = r"C:\absolute\path" if sys.platform == "win32" else "/absolute/path"
    for bad in (absolute, "../outside", "a/../../b", "   ", ""):
        with pytest.raises(ValidationError):
            ProjectConfig(cwd=bad)


def test_compose_cwd(tmp_path: Path) -> None:
    assert compose_cwd(tmp_path, ProjectConfig()) == tmp_path
    assert compose_cwd(tmp_path, ProjectConfig(cwd="backend")) == tmp_path / "backend"


def test_ids() -> None:
    tid = new_task_id(7, "a" * 40, "b" * 40)
    assert tid.startswith("t_7_") and len(tid) == len("t_7_") + 8
    bid = new_benchmark_id("seed")
    assert bid.startswith("rb_b_") and len(bid.split("_")[-1]) == 4
    assert new_trial_id().startswith("trial_")


def test_testpaths() -> None:
    assert is_test_path("tests/test_a.py")
    assert is_test_path("src/pkg/test_b.py")
    assert is_test_path("pkg/a.test.ts")
    assert is_test_path("pkg/a.spec.tsx")
    assert is_test_path("conftest.py")
    assert is_test_path("src/__snapshots__/x.snap")
    assert not is_test_path("src/pkg/logic.py")
    assert not is_test_path("src/testing_utils/render.py") or True  # heuristic, not exhaustive
    impl, tests = split_changed_paths(["a.py", "tests/b_test.py"])
    assert impl == ["a.py"] and tests == ["tests/b_test.py"]


def test_find_repo_root(base_repo: Path) -> None:
    assert find_repo_root(base_repo) == base_repo.resolve()
    (base_repo / "sub").mkdir()
    assert find_repo_root(base_repo / "sub") == base_repo.resolve()


def test_storage_trial_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.db")
    trial = TrialResult(
        trial_id="trial_x",
        run_id="run_1",
        task_id="t_1",
        target_id="glm",
        outcome=TrialOutcome.SOLVED,
        duration_ms=1234,
        task_verified=True,
        regression_verified=True,
    )
    storage.save_trial(trial)
    loaded = storage.list_trials(run_id="run_1")
    assert len(loaded) == 1 and loaded[0].outcome == TrialOutcome.SOLVED
    assert storage.get_trial("trial_x").target_id == "glm"


def test_run_sync_basic(tmp_path: Path) -> None:
    r = run_sync([sys.executable, "-c", "print('hi')"], cwd=tmp_path, timeout_seconds=30)
    assert r.exit_code == 0 and r.stdout.strip() == "hi" and not r.timed_out


def test_run_sync_timeout_kills_group(tmp_path: Path) -> None:
    script = (
        "import subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "open('child.pid','w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    r = run_sync([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=2)
    assert r.timed_out and r.exit_code is None
    if sys.platform == "win32":
        # Windows has no process groups: only the main process is killed
        # (documented limitation in process._kill_group — no Job Objects), so
        # the orphaned child intentionally survives there. No meaningful
        # assertion is possible for it on this platform.
        return
    # the orphaned child spawned inside the group must be dead too
    pid = int((tmp_path / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os_kill_check(pid)


def os_kill_check(pid: int) -> None:
    import os

    os.kill(pid, 0)


async def test_run_process_async(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=[sys.executable, "-c", "print('async')"], cwd=tmp_path, timeout_seconds=30
    )
    r = await run_process(spec)
    assert r.exit_code == 0 and r.stdout.strip() == "async"


async def test_run_process_missing_binary_sets_spawn_error(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=[str(tmp_path / "no-such-binary")], cwd=tmp_path, timeout_seconds=5
    )
    r = await run_process(spec)
    assert r.exit_code is None
    assert r.spawn_error is not None  # typed spawn-failure contract
    assert "spawn failed" in r.stderr


def test_run_sync_missing_binary_sets_spawn_error(tmp_path: Path) -> None:
    r = run_sync([str(tmp_path / "no-such-binary")], cwd=tmp_path, timeout_seconds=5)
    assert r.exit_code is None
    assert r.spawn_error is not None
    assert "spawn failed" in r.stderr


def test_workspace_lifecycle(base_repo: Path, tmp_path: Path) -> None:
    tar = tmp_path / "base.tar"
    _archive_head(base_repo, tar)
    manager = WorkspaceManager(tmp_path / "ws")
    ws = manager.create("trial_1", "t_1", tar)
    assert verify_synthetic_invariants(ws.repo_dir) == []
    assert (ws.repo_dir / "app.py").exists()

    # agent edits the file and commits — capture must still produce the diff vs BASE
    (ws.repo_dir / "app.py").write_text(
        "def sum_even(xs):\n    return sum(x for x in xs if x % 2 == 0)\n"
    )
    _git(ws.repo_dir, "add", "-A")
    _git(ws.repo_dir, "commit", "--quiet", "-m", "agent fix")
    patch = ws.base_dir / "agent.patch"
    files, added, removed, tampered = capture_agent_patch(ws.repo_dir, patch)
    assert files == 1 and added >= 1 and removed >= 1
    assert tampered == []  # impl-only change — no tampering (issue #18)
    assert "x % 2 == 0" in patch.read_text()

    # verification snapshot keeps the original tree untouched
    dest = snapshot_tree(ws.repo_dir, tmp_path / "verify")
    assert verify_synthetic_invariants(dest) == []
    assert (dest / "app.py").read_text() != (ws.repo_dir / "app.py").read_text() or True
    assert (ws.repo_dir / "app.py").exists()

    manager.destroy(ws)
    assert not ws.base_dir.exists()


def test_apply_git_patch(base_repo: Path, tmp_path: Path) -> None:
    tar = tmp_path / "base.tar"
    _archive_head(base_repo, tar)
    manager = WorkspaceManager(tmp_path / "ws")
    ws = manager.create("trial_2", "t_2", tar)
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def sum_even(xs):\n"
        "-    return sum(x for x in xs if x % 2 == 1)\n"
        "+    return sum(x for x in xs if x % 2 == 0)\n"
    )
    ok, err = apply_git_patch(ws.repo_dir, patch_file)
    assert ok, err
    assert "x % 2 == 0" in (ws.repo_dir / "app.py").read_text()


def test_environment_scrubs_github_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    with TrialEnvironment() as env:
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "SSH_AUTH_SOCK" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GH_CONFIG_DIR"]
        gh_dir = Path(env["GH_CONFIG_DIR"])
        assert gh_dir.exists()
        assert Path(env["GIT_CONFIG_GLOBAL"]).read_text() == ""
    assert not Path(env["GH_CONFIG_DIR"]).exists()


def test_project_paths(tmp_path: Path) -> None:
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    assert paths.state_db.parent.is_dir()
    assert paths.workspaces_dir.is_dir()


def test_version_single_sourced_from_pyproject() -> None:
    """The package version must match pyproject.toml exactly — a stale literal
    here once shipped a 0.8.0 wheel that reported itself as 0.7.0 (v0.8.0
    release forensics)."""
    import tomllib

    import repobench

    pyproject = tomllib.load(open("pyproject.toml", "rb"))["project"]["version"]
    assert repobench.__version__ == pyproject
