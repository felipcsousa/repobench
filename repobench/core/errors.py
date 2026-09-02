"""Base errors for RepoBench."""


class RepoBenchError(Exception):
    """Base error for RepoBench."""


class ConfigError(RepoBenchError):
    """Raised when repobench.yml is missing or invalid."""


class ReconstructionError(RepoBenchError):
    """Raised when a candidate's history cannot be reconstructed into a task
    package (missing SHAs, empty-tree base, `git archive` failure)."""


class UsageError(RepoBenchError):
    """Raised when the CLI is used incorrectly (bad target id, no benchmark, ...)."""
