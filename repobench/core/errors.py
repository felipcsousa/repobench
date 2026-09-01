"""Base errors for RepoBench."""


class RepoBenchError(Exception):
    """Base error for RepoBench."""


class ConfigError(RepoBenchError):
    """Raised when repobench.yml is missing or invalid."""


class UsageError(RepoBenchError):
    """Raised when the CLI is used incorrectly (bad target id, no benchmark, ...)."""
