"""Local process runner (PRD §39-40): spawn, capture, measure wall time, apply timeout,
terminate the process group, kill remaining children. Harness exit code is recorded but
never defines correctness (PRD §42).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from repobench.core.types import CommandSpec, ProcessResult

MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # cap captured output so results stay sane
TERMINATE_GRACE_SECONDS = 8.0


def _truncate(data: bytes) -> str:
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[-MAX_OUTPUT_BYTES:]
    return data.decode("utf-8", errors="replace")


def _new_session_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_group(pid: int, sig: int) -> None:
    # Each trial runs in its own process group so harness-spawned children
    # (shells, MCP servers, test runners) die with it (PRD §40).
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def run_process(spec: CommandSpec) -> ProcessResult:
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(spec.cwd),
            env=(spec.env or None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=(
                asyncio.subprocess.PIPE
                if spec.stdin is not None
                else asyncio.subprocess.DEVNULL
            ),
            **_new_session_kwargs(),
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        return ProcessResult(
            exit_code=None, stderr=f"spawn failed: {exc}", spawn_error=str(exc)
        )

    async def _read(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except asyncio.CancelledError:
            # Draining was cancelled (e.g. a daemonized child still holds the
            # pipe): return the chunks already collected instead of losing them.
            pass
        return b"".join(chunks)

    out_task = asyncio.create_task(_read(proc.stdout))
    err_task = asyncio.create_task(_read(proc.stderr))

    feed_task: asyncio.Task | None = None
    if spec.stdin is not None:
        async def _feed() -> None:
            try:
                proc.stdin.write(spec.stdin.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, RuntimeError):
                    pass

        feed_task = asyncio.create_task(_feed())

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=spec.timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            _kill_group(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def _collect(task: asyncio.Task | None) -> bytes:
        if task is None:
            return b""
        try:
            return await asyncio.wait_for(task, timeout=15)
        except asyncio.TimeoutError:
            # Drain window elapsed (a daemonized child can hold the pipe open).
            # Cancelling makes _read return the chunks it already collected.
            task.cancel()
            try:
                return await task
            except asyncio.CancelledError:
                return b""

    stdout_b = await _collect(out_task)
    stderr_b = await _collect(err_task)
    if feed_task is not None:
        try:
            await asyncio.wait_for(feed_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            feed_task.cancel()

    duration_ms = int((time.monotonic() - start) * 1000)
    return ProcessResult(
        exit_code=(None if timed_out else proc.returncode),
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=_truncate(stdout_b),
        stderr=_truncate(stderr_b),
    )


def run_sync(
    argv: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 600,
    stdin: str | None = None,
) -> ProcessResult:
    """Synchronous convenience wrapper used for setup, git plumbing and verifiers."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=(subprocess.PIPE if stdin is not None else subprocess.DEVNULL),
            **_new_session_kwargs(),
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        return ProcessResult(
            exit_code=None, stderr=f"spawn failed: {exc}", spawn_error=str(exc)
        )

    timed_out = False
    try:
        out, err = proc.communicate(
            stdin.encode() if stdin is not None else None, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()

    duration_ms = int((time.monotonic() - start) * 1000)
    return ProcessResult(
        exit_code=(None if timed_out else proc.returncode),
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=_truncate(out or b""),
        stderr=_truncate(err or b""),
    )
