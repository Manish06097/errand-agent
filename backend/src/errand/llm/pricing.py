"""Model prices and the cost formula (spike finding C6).

One usage source only: LangChain's `usage_metadata`. Its `input_tokens` already
includes cache reads and cache writes, so they are subtracted before pricing. When the
response carries the TTL split, LangChain reports writes only in
`ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` and zeroes the generic
`cache_creation`; the generic field is used only when both TTL fields are 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from errand.usage import Usage

# Costs are rounded to this many decimal places. A model call is never cheaper than
# 1e-10 USD of anything, and Decimal keeps the run totals exact.
COST_PLACES = Decimal("1e-10")
_PER_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens, plus cache multipliers on the input price."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_mult: Decimal
    cache_write_5m_mult: Decimal = Decimal("1.25")
    cache_write_1h_mult: Decimal = Decimal("2")


# The table from spikes/claude/s4_usage_cache.py (architecture 16.2).
PRICES: Mapping[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(Decimal("4"), Decimal("20"), cache_read_mult=Decimal("0.05")),
    "claude-haiku-5-5": ModelPrice(
        Decimal("0.10"), Decimal("0.50"), cache_read_mult=Decimal("0.10")
    ),
}


class UnknownModelPrice(KeyError):
    """Raised instead of pricing an unknown model at zero."""


def _int(details: Mapping[str, Any], key: str) -> int:
    value = details.get(key) or 0
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"usage field {key!r} is not a non-negative int: {value!r}")
    return value


def usage_from_metadata(usage_metadata: Mapping[str, Any]) -> Usage:
    """Split LangChain `usage_metadata` into priced parts."""
    details: Mapping[str, Any] = usage_metadata.get("input_token_details") or {}
    read = _int(details, "cache_read")
    write_5m = _int(details, "ephemeral_5m_input_tokens")
    write_1h = _int(details, "ephemeral_1h_input_tokens")
    if write_5m == 0 and write_1h == 0:
        # No TTL split in the response: the generic field is all 5 minute writes.
        write_5m = _int(details, "cache_creation")
    input_tokens = _int(usage_metadata, "input_tokens")
    uncached = input_tokens - read - write_5m - write_1h
    if uncached < 0:
        raise ValueError(
            f"usage is inconsistent: input_tokens {input_tokens} is less than cache reads "
            f"and writes ({read} + {write_5m} + {write_1h})"
        )
    out_details: Mapping[str, Any] = usage_metadata.get("output_token_details") or {}
    return Usage(
        input_uncached=uncached,
        cache_read=read,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        output=_int(usage_metadata, "output_tokens"),
        reasoning=_int(out_details, "reasoning"),
    )


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelPrice(f"no price for model {model!r}; add it to PRICES") from None


def cost_decimal(model: str, usage: Usage) -> Decimal:
    p = price_for(model)
    total = (
        usage.input_uncached * p.input_per_mtok
        + usage.cache_read * p.input_per_mtok * p.cache_read_mult
        + usage.cache_write_5m * p.input_per_mtok * p.cache_write_5m_mult
        + usage.cache_write_1h * p.input_per_mtok * p.cache_write_1h_mult
        + usage.output * p.output_per_mtok
    ) / _PER_MTOK
    return total.quantize(COST_PLACES)


def cost_usd(model: str, usage: Usage) -> float:
    return float(cost_decimal(model, usage))
