"""RunCounters: the one counter type for a run.

The model gateway and the tool gateway update the same instance while a run is live,
which makes them authoritative; `plan` copies it into `ThreadState.counters` when it
ends. There is no second copy that could drift.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RunCounters(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    model_calls: int = 0
    tool_calls: int = 0
    reprompts: int = 0
    replans: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Field(default_factory=Decimal)
    # Set by the RunManager from the injected clock; None only for gateways built
    # outside a run (unit tests).
    started_at: datetime | None = None
    deadline_at: datetime | None = None

    @property
    def cost_usd(self) -> float:
        return float(self.cost)

    def time_left_s(self, now: datetime) -> float | None:
        if self.deadline_at is None:
            return None
        return (self.deadline_at - now).total_seconds()
