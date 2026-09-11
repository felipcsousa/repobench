"""Execution-target renderers (PRD §93, §120; the issue #17 pricing column)."""

from __future__ import annotations

from rich.table import Table as RichTable

from repobench.cli.render.base import OK, console
from repobench.config import RepoBenchConfig
from repobench.core.types import ExecutionTarget
from repobench.execution.pricing_catalog import lookup as catalog_lookup


def _target_provider(target: ExecutionTarget) -> str:
    """Explicit provider field, or the model's "provider/model" prefix, else inherited."""
    if target.provider:
        return target.provider
    if target.model and "/" in target.model:
        return target.model.split("/", 1)[0]
    return "inherited"


def _target_pricing_label(target: ExecutionTarget, cfg: RepoBenchConfig) -> str:
    """Issue #17: where the target's cost will come from — the user's
    `pricing:` rule, the bundled catalog (`~` marks an estimate), or nothing."""
    if target.model and target.model in cfg.pricing:
        return "user"
    if catalog_lookup(target.model) is not None:
        return "catalog~"
    return "—"


def render_targets_table(cfg: RepoBenchConfig) -> None:
    table = RichTable(header_style="bold")
    for column in ("TARGET", "HARNESS", "MODEL", "PROVIDER", "PRICING"):
        table.add_column(column)
    for target in cfg.targets.values():
        table.add_row(
            target.id,
            target.harness,
            target.model or "(harness default)",
            _target_provider(target),
            _target_pricing_label(target, cfg),
        )
    console.print(table)


def provider_label(target: ExecutionTarget) -> str:
    return _target_provider(target)


def render_capability_row(name: str, caps) -> str:  # noqa: ANN001 - HarnessCapabilities
    def mark(flag: bool) -> str:
        return OK if flag else "✗"

    name_column = 12
    return (
        f"{name:<{name_column}}"
        f"{mark(caps.model_override):^7}"
        f"{mark(caps.structured_output):^7}"
        f"{mark(caps.token_usage):^7}"
        f"{mark(caps.cost_usage):^7}"
        f"{mark(caps.custom_provider):^10}"
    )
