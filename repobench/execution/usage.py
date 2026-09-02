"""Usage/cost resolution (PRD §53-56, issue #17).

Cost is attributed to exactly one source, never a mix without identification.
Precedence chain — the first step that can produce a cost wins:
1. HARNESS_REPORTED: the harness itself reported a cost;
2. USER_PROVIDED_PRICING: computed from the user's `pricing:` rule;
3. CATALOG_ESTIMATE: computed from the bundled pricing catalog snapshot
   (repobench/execution/pricing_catalog.py) — only when the caller resolved a
   catalog price for a known model; an estimate by definition;
4. (None, None): unknown — never invent usage or cost (PRD §54).
"""

from __future__ import annotations

from repobench.config import PricingRule
from repobench.core.types import UsageRecord
from repobench.execution.pricing_catalog import CatalogPrice


def _cost_from_prices(
    usage: UsageRecord,
    input_per_million: float,
    cached_input_per_million: float | None,
    output_per_million: float,
) -> float | None:
    input_tokens = usage.input_tokens
    cached_tokens = usage.cached_input_tokens
    output_tokens = usage.output_tokens
    if input_tokens is None and cached_tokens is None and output_tokens is None:
        # No token data at all: computing 0.0 would invent a cost (PRD §54).
        return None

    cost = (input_tokens or 0) * (input_per_million / 1_000_000)
    if cached_input_per_million is not None:
        cost += (cached_tokens or 0) * (cached_input_per_million / 1_000_000)
    cost += (output_tokens or 0) * (output_per_million / 1_000_000)
    return cost


def resolve_cost(
    usage: UsageRecord | None,
    pricing: PricingRule | None,
    catalog_price: CatalogPrice | None = None,
) -> tuple[float | None, str | None]:
    """Resolve the cost of a trial from usage + optional pricing sources.

    Returns (cost_usd, cost_source) following the precedence chain in the
    module docstring: harness-reported wins, then the user's pricing rule, then
    a catalog estimate (only when the caller passed one for a known model).
    When none is available the cost is unknown — it is never guessed (PRD §54,
    §56, issue #17).
    """
    if usage is None:
        return (None, None)
    if usage.reported_cost_usd is not None:
        return (usage.reported_cost_usd, "HARNESS_REPORTED")
    if pricing is not None:
        cost = _cost_from_prices(
            usage,
            pricing.input_per_million,
            pricing.cached_input_per_million,
            pricing.output_per_million,
        )
        if cost is not None:
            return (cost, "USER_PROVIDED_PRICING")
    if catalog_price is not None:
        cost = _cost_from_prices(
            usage,
            catalog_price.input_per_million,
            catalog_price.cached_input_per_million,
            catalog_price.output_per_million,
        )
        if cost is not None:
            return (cost, "CATALOG_ESTIMATE")
    return (None, None)


def total_tokens(usage: UsageRecord | None) -> int | None:
    """input + output tokens when both are present, else None."""
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    return usage.input_tokens + usage.output_tokens
