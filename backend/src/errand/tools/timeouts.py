"""The one timeout table (architecture 15.1).

Every client timeout comes from here, and the HTTP world learns the `book` timeout
from the client on each request (`CLIENT_TIMEOUT_HEADER`), so the "commit, then
hold the connection past the client's timeout" behaviour can never drift from the
client's value. Tests shrink values through settings, for example
`ToolTimeouts(book=0.5)` or `ERRAND_TIMEOUT_BOOK=0.5`.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand.world.contracts import ToolName


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
