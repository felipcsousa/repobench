"""RepoBench — repository-native evals for coding agents.

RepoBench turns a repository's real engineering history into a private, reproducible
benchmark suite, then runs the coding agents already installed and configured on the
local machine against it.
"""

from importlib.metadata import PackageNotFoundError, version

# Single-sourced from the installed package metadata (which hatchling derives
# from pyproject.toml) — a second literal here shipped a wheel that reported
# itself as 0.7.0 while PyPI said 0.8.0.
try:
    __version__ = version("repobench")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0.dev0"
