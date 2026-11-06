"""Token usage of one model call, split the way it is priced.

It sits outside both `errand.llm` and `errand.trace` because both need it: the gateway
builds it from a response and prices it, the tracer records it. Keeping it here lets the
tracer stay below the model gateway (import-linter pins that direction).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, NonNegativeInt


class Usage(BaseModel):
    """Token usage of one model call, split the way it is priced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_uncached: NonNegativeInt = 0
    cache_read: NonNegativeInt = 0
    cache_write_5m: NonNegativeInt = 0
    cache_write_1h: NonNegativeInt = 0
    output: NonNegativeInt = 0
    # A decomposition of `output`, not an extra term.
    reasoning: NonNegativeInt = 0

    @property
    def input_total(self) -> int:
        """Everything the request consumed as input, cached or not."""
        return self.input_uncached + self.cache_read + self.cache_write_5m + self.cache_write_1h
