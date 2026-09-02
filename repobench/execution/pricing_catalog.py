"""Bundled, dated, static pricing catalog (issue #17).

A deliberately SMALL snapshot of well-known model prices, used only as the
LAST step of cost resolution (see repobench/execution/usage.py): a
harness-reported cost wins, then the user's `pricing:` rule, and only when
neither exists does a catalog estimate apply — clearly labeled as
CATALOG_ESTIMATE with `estimate=True`, never presented as fact.

There is NO network access, ever. To extend the catalog, edit the snapshot
below (add or update keys, keep CATALOG_VERSION dated) and commit the change;
results stay reproducible because the prices ship with the code.

Catalog key discipline:
- Keys are FULL model ids (the ids adapters are typically given), never bare
  vendor/family names — a key like `minimax` would silently swallow
  `minimax-x`-style variants priced differently.
- Prefix variants: a dated or variant build that extends a key with a
  "-"-separated suffix (e.g. `claude-sonnet-4-5-20250929`) resolves to the key
  it extends, because it is the same billed model family entry.
"""

from __future__ import annotations

import pydantic

CATALOG_VERSION = "2026-09 snapshot"


class CatalogPrice(pydantic.BaseModel):
    """One catalog entry: USD per million tokens, always an estimate (issue #17)."""

    input_per_million: float
    cached_input_per_million: float | None = None
    output_per_million: float
    source: str = "catalog"
    estimate: bool = True


CATALOG: dict[str, CatalogPrice] = {
    "claude-sonnet-4-5": CatalogPrice(
        input_per_million=3.00, cached_input_per_million=0.30, output_per_million=15.00
    ),
    "claude-haiku-4-5": CatalogPrice(
        input_per_million=1.00, cached_input_per_million=0.10, output_per_million=5.00
    ),
    "claude-opus-4-1": CatalogPrice(
        input_per_million=15.00, cached_input_per_million=1.50, output_per_million=75.00
    ),
    "gpt-5.1": CatalogPrice(
        input_per_million=1.25, cached_input_per_million=0.125, output_per_million=10.00
    ),
    "gpt-5.1-codex": CatalogPrice(
        input_per_million=1.25, cached_input_per_million=0.125, output_per_million=10.00
    ),
    "gpt-5": CatalogPrice(
        input_per_million=1.25, cached_input_per_million=0.125, output_per_million=10.00
    ),
    "gpt-5-mini": CatalogPrice(
        input_per_million=0.25, cached_input_per_million=0.025, output_per_million=2.00
    ),
    "glm-4.6": CatalogPrice(
        input_per_million=0.60, cached_input_per_million=0.11, output_per_million=2.20
    ),
    "gemini-2.5-flash": CatalogPrice(
        input_per_million=0.30, output_per_million=2.50
    ),
    "gemini-2.5-pro": CatalogPrice(
        input_per_million=1.25, output_per_million=10.00
    ),
}


def _candidate_model_ids(model: str) -> list[str]:
    """Match candidates for one model string: the full id, then its final
    "/"-path segment (so `zai/glm-4.6` and bare `glm-4.6` both match the
    `glm-4.6` key). Vendor prefixes are never stripped from the key side."""
    lowered = model.strip().lower()
    candidates = [lowered]
    final_segment = lowered.rsplit("/", 1)[-1]
    if final_segment and final_segment not in candidates:
        candidates.append(final_segment)
    return candidates


def _matches(candidate: str, key: str) -> bool:
    """Exact match, or the candidate extends the key with a "-"-separated
    variant suffix (`claude-sonnet-4-5-20250929` matches `claude-sonnet-4-5`)."""
    return candidate == key or candidate.startswith(f"{key}-")


def lookup(model: str | None) -> CatalogPrice | None:
    """Longest-prefix catalog lookup for one model id (rule in the module
    docstring). None for unknown models — never a guessed price (issue #17)."""
    if not model or not model.strip():
        return None
    best: tuple[int, CatalogPrice] | None = None
    for candidate in _candidate_model_ids(model):
        for key, price in CATALOG.items():
            lowered_key = key.lower()
            if _matches(candidate, lowered_key):
                if best is None or len(lowered_key) > best[0]:
                    best = (len(lowered_key), price)
    return best[1] if best is not None else None
