"""The one timeout table (architecture 15.1).

Every client timeout comes from here, and the HTTP world learns the `book` timeout
from the client on each request (`CLIENT_TIMEOUT_HEADER`), so the "commit, then
hold the connection past the client's timeout" behaviour can never drift from the
client's value. Tests shrink values through settings, for example
`ToolTimeouts(book=0.5)` or `ERRAND_TIMEOUT_BOOK=0.5`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand.world.contracts import ToolName

# The ToolGateway caps each call's timeout by the run time left (15.1). It sets the cap
# for the duration of one call; the transports read it through `effective_timeout`.
_cap_s: ContextVar[float | None] = ContextVar("errand_tool_timeout_cap", default=None)


class ToolTimeouts(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_TIMEOUT_", frozen=True, extra="forbid")

    search_providers: float = Field(5.0, gt=0)
    get_provider: float = Field(5.0, gt=0)
    list_bookings: float = Field(5.0, gt=0)
    hold_slot: float = Field(5.0, gt=0)
    book: float = Field(8.0, gt=0)
    cancel: float = Field(5.0, gt=0)
    reschedule: float = Field(5.0, gt=0)
    get_booking_status: float = Field(3.0, gt=0)

    def for_tool(self, tool: ToolName) -> float:
        value: float = getattr(self, tool)
        return value


@contextmanager
def capped(seconds: float | None) -> Iterator[None]:
    """Cap every tool timeout at `seconds` inside the block (None: no cap)."""
    token = _cap_s.set(seconds)
    try:
        yield
    finally:
        _cap_s.reset(token)


def effective_timeout(timeouts: ToolTimeouts, tool: ToolName) -> float:
    table = timeouts.for_tool(tool)
    cap = _cap_s.get()
    return table if cap is None else max(0.001, min(table, cap))
