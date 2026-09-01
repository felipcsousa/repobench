"""Usage/cost resolution (PRD §53-56).

Cost is attributed to exactly one source, never a mix without identification:
- HARNESS_REPORTED: the harness itself reported a cost;
- USER_PROVIDED_PRICING: computed from the user's pricing table;
- (None, None): unknown — never invent usage or cost (PRD §54).
"""

from __future__ import annotations

from repobench.config import PricingRule
from repobench.core.types import UsageRecord


def resolve_cost(
    usage: UsageRecord | None, pricing: PricingRule | None
) -> tuple[float | None, str | None]:
    """Resolve the cost of a trial from usage + optional pricing rule.

    Returns (cost_usd, cost_source). A harness-reported cost always wins; pricing
    is only used when the harness did not report a cost. When neither is available
    the cost is unknown — it is never guessed (PRD §54, §56).
    """
    if usage is None:
        return (None, None)
    if usage.reported_cost_usd is not None:
        return (usage.reported_cost_usd, "HARNESS_REPORTED")
    if pricing is None:
        return (None, None)

    input_tokens = usage.input_tokens
    cached_tokens = usage.cached_input_tokens
    output_tokens = usage.output_tokens
    if input_tokens is None and cached_tokens is None and output_tokens is None:
        # No token data at all: computing 0.0 would invent a cost (PRD §54).
        return (None, None)

    cost = (input_tokens or 0) * (pricing.input_per_million / 1_000_000)
    if pricing.cached_input_per_million is not None:
        cost += (cached_tokens or 0) * (pricing.cached_input_per_million / 1_000_000)
    cost += (output_tokens or 0) * (pricing.output_per_million / 1_000_000)
    return (cost, "USER_PROVIDED_PRICING")


def total_tokens(usage: UsageRecord | None) -> int | None:
    """input + output tokens when both are present, else None."""
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    return usage.input_tokens + usage.output_tokens
