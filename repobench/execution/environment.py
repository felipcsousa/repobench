"""Trial environment sanitization (PRD §46-48).

Policy: inherit the host environment (the harness needs its provider auth), but scrub
credentials related to the benchmark source — GitHub tokens, SSH agent socket, credential
helpers and stored gh config — so a simple `gh pr view <N>` does not recover the solution.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

GITHUB_TOKEN_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


class TrialEnvironment:
    """Context manager producing the env dict for a trial subprocess plus a temporary,
    scrubbed GitHub/git configuration directory."""

    def __init__(self, *, scrub_ssh_agent: bool = True, extra_env: dict[str, str] | None = None):
        self.scrub_ssh_agent = scrub_ssh_agent
        self.extra_env = extra_env or {}
        self.temp_dir: Path | None = None

    def __enter__(self) -> dict[str, str]:
        env = dict(os.environ)
        for var in GITHUB_TOKEN_VARS:
            env.pop(var, None)
        if self.scrub_ssh_agent:
            env.pop("SSH_AUTH_SOCK", None)
        self.temp_dir = Path(tempfile.mkdtemp(prefix="repobench-trial-env-"))
        # Empty GitHub CLI config and empty git global config: blocks credential
        # helpers and stored tokens (PRD §47).
        (self.temp_dir / "gitconfig").write_text("")
        env["GH_CONFIG_DIR"] = str(self.temp_dir)
        env["GIT_CONFIG_GLOBAL"] = str(self.temp_dir / "gitconfig")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.update(self.extra_env)
        return env

    def __exit__(self, *exc) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None

