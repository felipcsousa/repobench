"""Logging setup. Library code logs to stderr; CLI output stays on stdout."""

from __future__ import annotations

import logging
import sys


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("repobench").setLevel(level)
