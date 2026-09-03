"""Runner tests: the Milestone-1 vertical slice (PRD §129, §141) exercised end-to-end
through the generic command adapter with hermetic fake agents (plain Python scripts run
with sys.executable — no real harness binary and no network)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from repobench.config import ExecutionConfig, ProjectConfig
from repobench.core.types import CommandSpec, ExecutionTarget, ProcessResult, TaskPackage, TrialOutcome
from repobench.execution import runner as runner_module
from repobench.execution.runner import TrialExecutor, build_task_prompt, run_matrix
from repobench.execution.testreport import JUNIT_FILENAME, TestCounts, invokes_pytest
from repobench.execution.workspace import WorkspaceManager

BASE_CALCULATOR = "def sum_even(xs):\n    return sum(x for x in xs if x % 2 == 1)\n"
FIXED_CALCULATOR = "def sum_even(xs):\n    return sum(x for x in xs if x % 2 == 0)\n"
TEST_CALC = (
    "from calculator import sum_even\n"
    "\n"
    "def test_sum_even():\n"
    "    assert sum_even([1, 2, 3, 4]) == 6\n"
)
INSTRUCTION = (
    "Fix sum_even in calculator.py: it currently sums the odd numbers of the input "
    "list. It must return the sum of the even numbers instead."
)

VERIFIER_PATCH = """\
diff --git a/test_hidden.py b/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/test_hidden.py
@@ -0,0 +1,4 @@
+from calculator import sum_even
+
+def test_sum_even():
+    assert sum_even([1, 2, 3, 4]) == 6
"""

GOLD_PATCH = """\
diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def sum_even(xs):
-    return sum(x for x in xs if x % 2 == 1)
+    return sum(x for x in xs if x % 2 == 0)
"""

FIX_AGENT = """\
import sys
from pathlib import Path

ws = Path(sys.argv[1])
p = ws / "calculator.py"
# Byte-exact edit: read_text/write_text would translate newlines (and depend on
# the locale codec) differently per platform, corrupting the fixture on Windows.
p.write_bytes(p.read_bytes().replace(b"x % 2 == 1", b"x % 2 == 0"))
print("fixed sum_even")
"""

NOOP_AGENT = """\
import sys
print("looks fine to me, no changes needed")
"""

SLOW_AGENT = """\
import sys, time
time.sleep(60)
"""

# Writes start/end monotonic timestamps around a sleep, into a shared marker
# dir, tagged by target id — used to prove real concurrency (or its absence).
TIMING_AGENT = """\
import sys, time
from pathlib import Path

marker_dir = Path(sys.argv[2])
name = sys.argv[3]
(marker_dir / f"{name}.start").write_text(repr(time.monotonic()))
time.sleep(1.5)
(marker_dir / f"{name}.end").write_text(repr(time.monotonic()))
"""

COMMIT_AGENT = """\
import subprocess, sys
from pathlib import Path

ws = Path(sys.argv[1])
p = ws / "calculator.py"
# Byte-exact edit (see FIX_AGENT): no newline/codec translation.
p.write_bytes(p.read_bytes().replace(b"x % 2 == 1", b"x % 2 == 0"))
git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.com"]
subprocess.run([*git, "add", "-A"], cwd=ws, check=True)
subprocess.run([*git, "commit", "--quiet", "-m", "fix sum_even"], cwd=ws, check=True)
"""


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_agent(tmp_path: Path, name: str, source: str) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    script = agents / name
    script.write_text(source)
    return script


def _make_task(base: Path, task_id: str = "t_calc") -> TaskPackage:
    """Build a task package: buggy calculator + (failing-on-base) test + hidden verifier."""
    history = base / "history"
    history.mkdir(parents=True)
    (history / "calculator.py").write_text(BASE_CALCULATOR)
    # Present in BASE but failing there — the hidden verifier semantics anchor.
    (history / "test_calc.py").write_text(TEST_CALC)
    _git(history, "init", "--quiet", "--initial-branch=main")
    _git(history, "add", "-A")
    _git(history, "commit", "--quiet", "-m", "initial")

    package = base / "task"
    package.mkdir()
    with (package / "base.tar").open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=history,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=True,
        )
    (package / "instruction.md").write_text(INSTRUCTION)
    (package / "verifier.patch").write_text(VERIFIER_PATCH)
    (package / "gold.patch").write_text(GOLD_PATCH)
    (package / "metadata.json").write_text(
        json.dumps({"task_id": task_id, "base_sha": "0" * 40, "gold_sha": "1" * 40})
    )
    return TaskPackage.load(package)


def _command_target(name: str, script: Path) -> ExecutionTarget:
    return ExecutionTarget(
        id=name,
        harness="command",
        command=[sys.executable, str(script), "{workspace}"],
    )


def _executor(
    tmp_path: Path,
    *,
    project_cfg: ProjectConfig | None = None,
    execution_cfg: ExecutionConfig | None = None,
    keep: bool = False,
    on_result=None,
    adapter_lookup=None,
) -> TrialExecutor:
    return TrialExecutor(
        workspaces=WorkspaceManager(tmp_path / "workspaces", keep=keep),
        execution_cfg=execution_cfg or ExecutionConfig(),
        project_cfg=project_cfg or ProjectConfig(test_command=f'"{sys.executable}" -m pytest -q'),
        artifacts_dir=tmp_path / "artifacts",
        on_result=on_result,
        adapter_lookup=adapter_lookup,
    )


def test_build_task_prompt_contains_workspace_instruction_and_constraints(tmp_path: Path) -> None:
    prompt = build_task_prompt(INSTRUCTION, tmp_path / "repo")
    assert str(tmp_path / "repo") in prompt
    assert INSTRUCTION in prompt  # verbatim
    lowered = prompt.lower()
    assert "only within this repository" in lowered
    assert "minimal correct change" in lowered
    assert "existing tests" in lowered
    assert "push" in lowered and "pull request" in lowered


async def test_execute_persists_final_prompt_artifact(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent.py", FIX_AGENT)
    executor = _executor(tmp_path)
    target = _command_target("fake-agent", fix_agent)

    result = await executor.execute(task, target)

    # The final composed prompt is a first-class artifact for every adapter.
    assert result.prompt_path is not None
    prompt_file = Path(result.prompt_path)
    assert prompt_file == tmp_path / "artifacts" / "trials" / result.trial_id / "prompt.md"
    assert prompt_file.is_file()
    text = prompt_file.read_text()
    assert "You are working in the repository" in text
    assert INSTRUCTION in text  # verbatim instruction
    assert "Constraints:" in text
    assert (prompt_file.parent / "trial.json").is_file()  # next to the other artifacts


async def test_execute_solved_end_to_end(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent.py", FIX_AGENT)
    observed: list = []
    executor = _executor(tmp_path, on_result=observed.append)
    target = _command_target("fake-agent", fix_agent)

    result = await executor.execute(task, target, run_id="run_1", benchmark_id="rb_b_1")

    assert result.outcome == TrialOutcome.SOLVED
    assert result.task_verified is True
    assert result.regression_verified is True
    assert result.exit_code == 0
    assert not result.timed_out
    assert (result.files_changed or 0) >= 1
    assert result.usage is None and result.cost_usd is None and result.cost_source is None
    assert result.harness == "command" and result.target_id == "fake-agent"
    assert result.task_id == task.task_id
    assert result.run_id == "run_1" and result.benchmark_id == "rb_b_1"

    # agent patch captured in the artifacts dir and contains the fix
    patch = Path(result.agent_patch)
    assert patch.is_file()
    assert "x % 2 == 0" in patch.read_text()

    # trial manifest written under artifacts/trials/<trial_id>/trial.json
    manifest = tmp_path / "artifacts" / "trials" / result.trial_id / "trial.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["outcome"] == "SOLVED"
    assert data["task_id"] == task.task_id

    # workspace destroyed after the trial
    assert not (tmp_path / "workspaces" / result.trial_id).exists()

    # on_result notified with the same result
    assert len(observed) == 1 and observed[0].trial_id == result.trial_id


async def test_execute_noop_agent_is_unsolved(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    noop = _write_agent(tmp_path, "noop_agent.py", NOOP_AGENT)
    executor = _executor(tmp_path)
    result = await executor.execute(task, _command_target("noop", noop))

    assert result.outcome == TrialOutcome.UNSOLVED
    assert result.task_verified is False
    assert result.regression_verified is None  # regression skipped when the task fails
    assert result.files_changed == 0
    assert result.error is None


async def test_execute_timeout_outcome(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    slow = _write_agent(tmp_path, "slow_agent.py", SLOW_AGENT)
    executor = _executor(tmp_path, execution_cfg=ExecutionConfig(timeout_seconds=2))
    result = await executor.execute(task, _command_target("slow", slow))

    assert result.outcome == TrialOutcome.TIMEOUT
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.task_verified is None and result.regression_verified is None
    # patch stats still recorded best-effort (agent changed nothing)
    assert result.files_changed == 0
    assert (tmp_path / "workspaces" / result.trial_id).exists() is False


async def test_execute_agent_commits_are_still_captured(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    committer = _write_agent(tmp_path, "commit_agent.py", COMMIT_AGENT)
    executor = _executor(tmp_path)
    result = await executor.execute(task, _command_target("committer", committer))

    assert result.outcome == TrialOutcome.SOLVED
    assert result.files_changed == 1
    assert result.loc_added >= 1 and result.loc_removed >= 1
    assert "x % 2 == 0" in Path(result.agent_patch).read_text()


async def test_execute_keeps_workspace_and_prompt_file_outside_repo(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent_keep.py", FIX_AGENT)
    executor = _executor(tmp_path, keep=True)
    target = ExecutionTarget(
        id="fake-agent",
        harness="command",
        # uses {prompt_file}: the adapter must write the prompt next to the repo
        command=[sys.executable, str(fix_agent), "{workspace}", "--prompt-file", "{prompt_file}"],
    )
    result = await executor.execute(task, target)

    assert result.outcome == TrialOutcome.SOLVED
    kept = tmp_path / "workspaces" / result.trial_id
    assert kept.is_dir()  # keep_workspaces preserves the trial dir
    prompt_file = kept / "prompt.md"
    assert prompt_file.is_file()  # written next to the repo, in the trial dir
    assert INSTRUCTION in prompt_file.read_text()
    assert not (kept / "repo" / "prompt.md").exists()  # never inside the repository
    assert "prompt.md" not in Path(result.agent_patch).read_text()  # and never in the patch


async def test_execute_missing_harness_binary_is_setup_error(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    executor = _executor(tmp_path)
    missing = tmp_path / "no-such-harness"
    target = ExecutionTarget(
        id="missing", harness="command", command=[str(missing), "{workspace}"]
    )
    result = await executor.execute(task, target)

    # spawn failure is detected through the typed ProcessResult.spawn_error field
    assert result.outcome == TrialOutcome.SETUP_ERROR
    assert "harness could not be started" in (result.error or "")
    if sys.platform != "win32":
        # POSIX spawn errors carry the binary path; Windows' "[WinError 2] The
        # system cannot find the file specified" does not name it.
        assert str(missing) in (result.error or "")
    assert result.exit_code is None
    assert result.task_verified is None
    assert not (tmp_path / "workspaces" / result.trial_id).exists()


async def test_execute_install_failure_is_setup_error(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent_install.py", FIX_AGENT)
    project_cfg = ProjectConfig(
        install_command=f'"{sys.executable}" -c "import sys; sys.exit(3)"',
        test_command=f'"{sys.executable}" -m pytest -q',
    )
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("fake-agent", fix_agent))

    assert result.outcome == TrialOutcome.SETUP_ERROR
    assert "install command failed" in (result.error or "")
    assert result.task_verified is None


async def test_execute_synthetic_invariant_violation_is_setup_error(tmp_path: Path, monkeypatch) -> None:
    """PRD §35 is enforced on real trials: a workspace that carries an original
    remote/branch must fail setup with the violations in the error message."""
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent_invariant.py", FIX_AGENT)
    executor = _executor(tmp_path)
    manager = executor.workspaces
    original_create = manager.create

    def leaky_create(trial_id: str, task_id: str, base_archive):
        ws = original_create(trial_id, task_id, base_archive)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/leak.git"],
            cwd=ws.repo_dir, check=True, capture_output=True,
        )
        return ws

    monkeypatch.setattr(manager, "create", leaky_create)
    result = await executor.execute(task, _command_target("fake-agent", fix_agent))

    assert result.outcome == TrialOutcome.SETUP_ERROR
    assert "synthetic invariants violated" in (result.error or "")
    assert "origin" in (result.error or "")
    assert not (tmp_path / "workspaces" / result.trial_id).exists()


async def test_execute_never_raises_on_adapter_lookup_failure(tmp_path: Path) -> None:
    from repobench.core.errors import UsageError

    task = _make_task(tmp_path)
    executor = _executor(tmp_path, adapter_lookup=lambda harness: (_ for _ in ()).throw(UsageError("boom")))
    result = await executor.execute(task, _command_target("x", tmp_path / "whatever.py"))

    assert result.outcome == TrialOutcome.SETUP_ERROR
    assert "boom" in (result.error or "")


async def test_run_matrix_jobs_one_returns_full_matrix_in_order(tmp_path: Path) -> None:
    tasks = [_make_task(tmp_path / f"task{i}", task_id=f"t_{i}") for i in range(2)]
    fix_agent = _write_agent(tmp_path, "fix_matrix.py", FIX_AGENT)
    noop = _write_agent(tmp_path, "noop_matrix.py", NOOP_AGENT)
    executor = _executor(tmp_path)
    targets = [_command_target("solver", fix_agent), _command_target("noop", noop)]
    pairs = [(task, target, 1) for task in tasks for target in targets]

    progress: list = []
    results = await run_matrix(
        pairs,
        executor,
        run_id="run_m",
        benchmark_id="rb_b_m",
        jobs=1,
        progress=progress.append,
    )

    assert len(results) == 4
    assert len(progress) == 4
    # jobs=1: completion order equals submission order (tasks outer, targets inner)
    assert [(r.task_id, r.target_id) for r in results] == [
        ("t_0", "solver"),
        ("t_0", "noop"),
        ("t_1", "solver"),
        ("t_1", "noop"),
    ]
    by_target = {}
    for trial in results:
        by_target.setdefault(trial.target_id, []).append(trial.outcome)
    assert by_target["solver"] == [TrialOutcome.SOLVED, TrialOutcome.SOLVED]
    assert by_target["noop"] == [TrialOutcome.UNSOLVED, TrialOutcome.UNSOLVED]
    assert all(trial.run_id == "run_m" and trial.benchmark_id == "rb_b_m" for trial in results)


# ------------------------------------------------------- new outcome contracts


async def test_execute_verifier_crash_is_verifier_error(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent_crash.py", FIX_AGENT)
    project_cfg = ProjectConfig(test_command=f'"{sys.executable}" -c "import sys; sys.exit(2)"')
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("crashy-verifier", fix_agent))

    assert result.outcome == TrialOutcome.VERIFIER_ERROR
    assert result.task_verified is None
    assert result.regression_verified is None
    assert "verifier crashed (exit 2)" in (result.error or "")


async def test_execute_regression_failure_after_task_passes_is_unsolved(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "fix_agent_regression.py", FIX_AGENT)
    project_cfg = ProjectConfig(
        test_command=f'"{sys.executable}" -m pytest -q',  # hidden verifier passes after the fix
        regression_command=f'"{sys.executable}" -c "import sys; sys.exit(1)"',
    )
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("regressing", fix_agent))

    assert result.outcome == TrialOutcome.UNSOLVED
    assert result.task_verified is True
    assert result.regression_verified is False
    assert result.error is None


# ------------------------------------------------- project.cwd knob (issue #34)


MONO_GOLD_PATCH = """\
diff --git a/backend/calculator.py b/backend/calculator.py
--- a/backend/calculator.py
+++ b/backend/calculator.py
@@ -1,2 +1,2 @@
 def sum_even(xs):
-    return sum(x for x in xs if x % 2 == 1)
+    return sum(x for x in xs if x % 2 == 0)
"""

# The hidden verifier itself asserts the two cwd contracts (issue #34): it only
# passes when pytest ran from backend/ (project.cwd) and the install command
# left its marker there — run at the repo root instead, the trial goes UNSOLVED.
MONO_VERIFIER_PATCH = """\
diff --git a/backend/test_hidden.py b/backend/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/backend/test_hidden.py
@@ -0,0 +1,13 @@
+import os
+from pathlib import Path
+
+from calculator import sum_even
+
+
+def test_sum_even():
+    assert sum_even([1, 2, 3, 4]) == 6
+
+
+def test_runs_in_the_configured_project_cwd():
+    assert os.path.basename(os.getcwd()) == "backend"
+    assert Path("install_marker.txt").is_file()
"""

MONO_FIX_AGENT = """\
import sys
from pathlib import Path

ws = Path(sys.argv[1])
# The harness always receives the workspace root (issue #34) — the agent must
# see the whole repo even when project.cwd points the verifiers at backend/.
assert (ws / "backend" / "calculator.py").is_file(), "harness cwd must be the repo root"
p = ws / "backend" / "calculator.py"
# Byte-exact edit (see FIX_AGENT): no newline/codec translation.
p.write_bytes(p.read_bytes().replace(b"x % 2 == 1", b"x % 2 == 0"))
print("fixed backend sum_even")
"""


def _make_monorepo_task(base: Path, task_id: str = "t_mono") -> TaskPackage:
    """_make_task with the project living in backend/: only a verifier run with
    project.cwd='backend' finds the tests, so the SOLVED verdict itself proves
    the cwd composition."""
    history = base / "history"
    (history / "backend").mkdir(parents=True)
    (history / "backend" / "calculator.py").write_text(BASE_CALCULATOR)
    (history / "backend" / "test_calc.py").write_text(TEST_CALC)
    _git(history, "init", "--quiet", "--initial-branch=main")
    _git(history, "add", "-A")
    _git(history, "commit", "--quiet", "-m", "initial")

    package = base / "task"
    package.mkdir()
    with (package / "base.tar").open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=history,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=True,
        )
    (package / "instruction.md").write_text(INSTRUCTION)
    (package / "verifier.patch").write_text(MONO_VERIFIER_PATCH)
    (package / "gold.patch").write_text(MONO_GOLD_PATCH)
    (package / "metadata.json").write_text(
        json.dumps({"task_id": task_id, "base_sha": "0" * 40, "gold_sha": "1" * 40})
    )
    return TaskPackage.load(package)


async def test_execute_project_cwd_runs_install_and_verifier_in_subdir(tmp_path: Path) -> None:
    """Issue #34: install and verifiers run in project.cwd inside the workspace
    while the harness still receives the repo root (asserted by the agent)."""
    task = _make_monorepo_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "mono_fix_agent.py", MONO_FIX_AGENT)
    project_cfg = ProjectConfig(
        cwd="backend",
        install_command=f'"{sys.executable}" -c "open(\'install_marker.txt\', \'w\').write(\'x\')"',
        test_command=f'"{sys.executable}" -m pytest -q',
    )
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("mono-agent", fix_agent))

    assert result.outcome == TrialOutcome.SOLVED, result.error
    assert result.task_verified is True
    assert result.regression_verified is True


async def test_execute_missing_project_cwd_verifier_is_verifier_error(tmp_path: Path) -> None:
    """A workspace without project.cwd is a config problem: VERIFIER_ERROR with
    a clear message — never a crash, never a silent UNSOLVED."""
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "mono_missing_agent.py", FIX_AGENT)
    project_cfg = ProjectConfig(cwd="no_such_dir", test_command=f'"{sys.executable}" -m pytest -q')
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("missing-cwd", fix_agent))

    assert result.outcome == TrialOutcome.VERIFIER_ERROR
    assert "project.cwd" in (result.error or "")
    assert "no_such_dir" in (result.error or "")


async def test_execute_missing_project_cwd_install_is_setup_error(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fix_agent = _write_agent(tmp_path, "mono_missing_install.py", FIX_AGENT)
    project_cfg = ProjectConfig(
        cwd="no_such_dir",
        install_command="echo hi",
        test_command=f'"{sys.executable}" -m pytest -q',
    )
    executor = _executor(tmp_path, project_cfg=project_cfg)
    result = await executor.execute(task, _command_target("missing-cwd", fix_agent))

    assert result.outcome == TrialOutcome.SETUP_ERROR
    assert "project.cwd" in (result.error or "")
    assert result.task_verified is None


# ----------------------------------------------------------- concurrency model


def _timing_target(name: str, script: Path, markers: Path) -> ExecutionTarget:
    return ExecutionTarget(
        id=name,
        harness="command",
        command=[sys.executable, str(script), "{workspace}", str(markers), "{target_id}"],
    )


def _read_interval(markers: Path, name: str) -> tuple[float, float]:
    start = float((markers / f"{name}.start").read_text())
    end = float((markers / f"{name}.end").read_text())
    return start, end


def _timing_pair_setup(tmp_path: Path):
    task_a = _make_task(tmp_path / "taskA", task_id="t_a")
    task_b = _make_task(tmp_path / "taskB", task_id="t_b")
    timing_agent = _write_agent(tmp_path, "timing_agent.py", TIMING_AGENT)
    markers = tmp_path / "markers"
    markers.mkdir()
    targets = [
        _timing_target("agent-a", timing_agent, markers),
        _timing_target("agent-b", timing_agent, markers),
    ]
    # fast failing verifier keeps these trials hermetic and quick
    executor = _executor(
        tmp_path, project_cfg=ProjectConfig(test_command=f'"{sys.executable}" -c "import sys; sys.exit(1)"')
    )
    pairs = [(task_a, targets[0], 1), (task_b, targets[1], 1)]
    return pairs, executor, markers


async def test_run_matrix_jobs_two_trials_overlap(tmp_path: Path) -> None:
    pairs, executor, markers = _timing_pair_setup(tmp_path)

    results = await run_matrix(pairs, executor, jobs=2)

    assert len(results) == 2
    a_start, a_end = _read_interval(markers, "agent-a")
    b_start, b_end = _read_interval(markers, "agent-b")
    # the two 1.5s harness intervals overlap: install/verify no longer block
    # the event loop, so --jobs 2 genuinely runs two trials at once
    assert max(a_start, b_start) < min(a_end, b_end)


async def test_run_matrix_jobs_one_serializes(tmp_path: Path) -> None:
    pairs, executor, markers = _timing_pair_setup(tmp_path)

    results = await run_matrix(pairs, executor, jobs=1)

    assert len(results) == 2
    a_start, a_end = _read_interval(markers, "agent-a")
    b_start, b_end = _read_interval(markers, "agent-b")
    # jobs=1: the second harness only starts after the first one finished
    assert min(a_end, b_end) <= max(a_start, b_start)


# ------------------------------------------------- partial credit (PRD §4)


# Hidden verifier with a 3-test suite over sum_even. Chosen so a "half-fixed"
# implementation passes exactly 2 of 3 (see PARTIAL_FIX_AGENT below).
TRIO_VERIFIER_PATCH = """\
diff --git a/test_trio.py b/test_trio.py
new file mode 100644
--- /dev/null
+++ b/test_trio.py
@@ -0,0 +1,13 @@
+from calculator import sum_even
+
+
+def test_one():
+    assert sum_even([1, 2]) == 2
+
+
+def test_two():
+    assert sum_even([3, 4]) == 4
+
+
+def test_three():
+    assert sum_even([2, 4]) == 6
"""

# Corrects the parity bug but drops the first element (off-by-one), so exactly
# one hidden test still fails: the red → partially-green anchor case.
PARTIAL_FIX_AGENT = """\
import sys
from pathlib import Path

ws = Path(sys.argv[1])
p = ws / "calculator.py"
p.write_text(
    "def sum_even(xs):\\n"
    "    return sum(xs[i] for i in range(1, len(xs)) if xs[i] % 2 == 0)\\n"
)
print("partially fixed sum_even")
"""

MINI_JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" failures="1" skipped="1">
    <testcase classname="test_trio" name="test_ok"/>
    <testcase classname="test_trio" name="test_bad">
        <failure message="assert">AssertionError</failure>
    </testcase>
    <testcase classname="test_trio" name="test_skip">
        <skipped message="not yet"/>
    </testcase>
</testsuite>
"""


def _make_trio_task(base: Path, task_id: str = "t_trio") -> TaskPackage:
    """Task whose hidden verifier is a 3-test suite (test_trio.py) on a base
    that also carries a pre-existing GREEN suite (test_green.py, 2 tests) —
    so hidden-scoped counts (3) are distinguishable from whole-suite counts
    (5) and the e2e actually proves the filter."""
    history = base / "history"
    history.mkdir(parents=True)
    (history / "calculator.py").write_text(BASE_CALCULATOR)
    (history / "test_green.py").write_text(
        "def test_green_one():\n    assert 1 + 1 == 2\n\n\ndef test_green_two():\n    assert [x for x in range(3)] == [0, 1, 2]\n"
    )
    _git(history, "init", "--quiet", "--initial-branch=main")
    _git(history, "add", "-A")
    _git(history, "commit", "--quiet", "-m", "initial")

    package = base / "task"
    package.mkdir()
    with (package / "base.tar").open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=history,
            stdout=fh,
            stderr=subprocess.PIPE,
            check=True,
        )
    (package / "instruction.md").write_text(INSTRUCTION)
    (package / "verifier.patch").write_text(TRIO_VERIFIER_PATCH)
    (package / "gold.patch").write_text(GOLD_PATCH)
    (package / "metadata.json").write_text(
        json.dumps({"task_id": task_id, "base_sha": "0" * 40, "gold_sha": "1" * 40})
    )
    return TaskPackage.load(package)


def _capture_run_process(monkeypatch, *, exit_code: int = 1, junit: str | None = None):
    """Replace runner.run_process with a fake that records every CommandSpec and,
    when junit is set, writes that XML at the path of the LAST --junitxml= token
    (mirroring pytest's last-wins report writing)."""
    captured: list[CommandSpec] = []

    async def fake_run_process(spec: CommandSpec) -> ProcessResult:
        captured.append(spec)
        if junit is not None:
            junit_tokens = [t for t in spec.argv if t.startswith("--junitxml=")]
            if junit_tokens:
                Path(junit_tokens[-1].split("=", 1)[1]).write_text(junit)
        return ProcessResult(exit_code=exit_code)

    monkeypatch.setattr(runner_module, "run_process", fake_run_process)
    return captured


async def test_execute_partial_fix_records_partial_credit_counts(tmp_path: Path) -> None:
    """PRD anchor case (red → partially green): the verdict stays exit-code-only
    UNSOLVED while the JUnit report records the per-test split beside it."""
    task = _make_trio_task(tmp_path)
    partial_agent = _write_agent(tmp_path, "partial_agent.py", PARTIAL_FIX_AGENT)
    executor = _executor(tmp_path)

    result = await executor.execute(task, _command_target("partial", partial_agent))

    assert result.outcome == TrialOutcome.UNSOLVED
    assert result.task_verified is False
    assert result.tests_passed == 2
    assert result.tests_failed == 1
    assert result.tests_skipped == 0
    assert result.tests_total == 3
    assert result.test_report_source == "pytest-junit"
    # Hidden-only denominator: the base's pre-existing green suite (2 tests in
    # test_green.py) ran in the same verifier process but must stay outside
    # the counts — whole-suite would read 2/5, not 2/3.
    # Honest trial duration (R2): total wall time > 0 (install+agent+verify)
    # and dominates the harness-only figure.
    assert result.duration_ms > 0
    assert result.harness_ms is not None
    assert result.harness_ms <= result.duration_ms


async def test_execute_verdict_identical_with_and_without_report(tmp_path: Path) -> None:
    """The same task, fully fixed: SOLVED under "auto" (counts 3/3) and SOLVED
    under "off" (fields None) — the report never moves the verdict."""
    agent = _write_agent(tmp_path, "full_fix_agent.py", FIX_AGENT)

    executor_auto = _executor(tmp_path)
    result_auto = await executor_auto.execute(
        _make_trio_task(tmp_path / "auto"), _command_target("full-auto", agent)
    )
    executor_off = _executor(
        tmp_path,
        project_cfg=ProjectConfig(
            test_command=f'"{sys.executable}" -m pytest -q', test_report="off"
        ),
    )
    result_off = await executor_off.execute(
        _make_trio_task(tmp_path / "off"), _command_target("full-off", agent)
    )

    assert result_auto.outcome == TrialOutcome.SOLVED
    assert result_auto.task_verified is True
    assert result_auto.tests_passed == 3
    assert result_auto.tests_failed == 0
    assert result_auto.tests_skipped == 0
    assert result_auto.tests_total == 3
    assert result_auto.test_report_source == "pytest-junit"

    assert result_off.outcome == TrialOutcome.SOLVED
    assert result_off.task_verified is True
    assert result_off.tests_passed is None
    assert result_off.tests_failed is None
    assert result_off.tests_skipped is None
    assert result_off.tests_total is None
    assert result_off.test_report_source is None

    # identical verdicts with and without the report — and identical to each other
    assert result_auto.outcome == result_off.outcome
    assert result_auto.task_verified == result_off.task_verified


async def test_run_verifier_off_keeps_argv_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """PRD acceptance: test_report="off" keeps the verifier argv byte-identical
    to the configured command — no flag appended, no counts invented."""
    captured = _capture_run_process(monkeypatch)
    executor = _executor(
        tmp_path,
        project_cfg=ProjectConfig(
            test_command=f'"{sys.executable}" -m pytest -q', test_report="off"
        ),
    )
    command = f'"{sys.executable}" -m pytest -q'

    verdict, counts = await executor._run_verifier(
        command, tmp_path, junit_path=tmp_path / JUNIT_FILENAME
    )

    assert verdict is False  # exit-1 semantics untouched
    assert counts is None
    assert captured[0].argv == shlex.split(command)


async def test_run_verifier_auto_appends_flag_and_parses_counts(tmp_path: Path, monkeypatch) -> None:
    captured = _capture_run_process(monkeypatch, junit=MINI_JUNIT)
    executor = _executor(tmp_path)  # default test_report="auto"
    command = f'"{sys.executable}" -m pytest -q'
    junit = tmp_path / JUNIT_FILENAME

    verdict, counts = await executor._run_verifier(command, tmp_path, junit_path=junit)

    assert verdict is False
    assert counts == TestCounts(passed=1, failed=1, skipped=1, total=3)
    assert captured[0].argv == [*shlex.split(command), f"--junitxml={junit}"]


async def test_run_verifier_auto_leaves_non_pytest_command_alone(
    tmp_path: Path, monkeypatch
) -> None:
    """A non-pytest verifier never gets the flag: counts stay None (honest)."""
    captured = _capture_run_process(monkeypatch)
    executor = _executor(tmp_path, project_cfg=ProjectConfig(test_command="npm test"))

    verdict, counts = await executor._run_verifier(
        "npm test", tmp_path, junit_path=tmp_path / JUNIT_FILENAME
    )

    assert verdict is False
    assert counts is None
    assert captured[0].argv == ["npm", "test"]


async def test_run_verifier_auto_user_junitxml_is_overridden_by_ours(
    tmp_path: Path, monkeypatch
) -> None:
    """A user --junitxml stays in the argv but ours is appended after it — and
    ours (last-wins) is the report that gets parsed."""
    junit = tmp_path / JUNIT_FILENAME
    captured = _capture_run_process(monkeypatch, junit=MINI_JUNIT)
    executor = _executor(tmp_path)
    command = f'"{sys.executable}" -m pytest --junitxml=proprio.xml'

    verdict, counts = await executor._run_verifier(command, tmp_path, junit_path=junit)

    assert verdict is False
    assert counts is not None and counts.total == 3  # our report was the one parsed
    argv = captured[0].argv
    assert argv.index("--junitxml=proprio.xml") < argv.index(f"--junitxml={junit}")


async def test_regression_fallback_run_gets_no_junit_flag(tmp_path: Path, monkeypatch) -> None:
    """PRD §4.2: the flag rides ONLY the task verifier invocation — the
    regression run stays flagless even when it falls back to the very same
    pytest-shaped test_command."""
    captured = _capture_run_process(monkeypatch, exit_code=0, junit=MINI_JUNIT)
    task = _make_trio_task(tmp_path)
    agent = _write_agent(tmp_path, "full_fix_agent.py", FIX_AGENT)
    executor = _executor(tmp_path)  # test_report "auto"; regression_command unset → fallback

    result = await executor.execute(task, _command_target("noflag-reg", agent))

    assert result.outcome == TrialOutcome.SOLVED  # both fake verifiers exited 0
    pytest_specs = [spec for spec in captured if invokes_pytest(spec.argv)]
    assert len(pytest_specs) == 2  # task verifier + regression fallback, nothing else
    flagged = [
        spec for spec in pytest_specs if any(t.startswith("--junitxml=") for t in spec.argv)
    ]
    assert len(flagged) == 1  # exactly the task verifier carries the flag
    assert result.tests_total == 3  # counts came from that single flagged run


async def test_run_verifier_auto_without_xml_keeps_counts_none(
    tmp_path: Path, monkeypatch
) -> None:
    """PRD edge case: exit 0 with no report file produced — verdict stands, the
    five fields stay None (nothing invented, no crash)."""
    captured = _capture_run_process(monkeypatch, exit_code=0)  # writes no XML
    executor = _executor(tmp_path)  # test_report "auto"
    command = f'"{sys.executable}" -m pytest -q'

    verdict, counts = await executor._run_verifier(
        command, tmp_path, junit_path=tmp_path / JUNIT_FILENAME
    )

    assert verdict is True
    assert counts is None
    assert not (tmp_path / JUNIT_FILENAME).exists()
    assert captured[0].argv[-1].startswith("--junitxml=")  # flag was attached, file wasn't
