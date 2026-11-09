"""The errand: one active flow per thread, with its phase (7.1, 7.2).

`phase` changes only through `errand.agent.transitions.transition`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand.domain.brief import Brief, ErrandKind
from errand.domain.slots import HoldRef, Shortlist


class Phase(StrEnum):
    GATHERING = "gathering"
    COMPARING = "comparing"
    QUOTING = "quoting"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class RebookInfo(BaseModel):
    """The cancelled booking a Rebook replaces, for the card's header and price delta."""

    model_config = ConfigDict(extra="forbid")

    booking_id: str
    provider_name: str
    slot_label: str
    price_inr: int | None = None  # what the user approved for it, when we know


class Errand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    errand_id: str
    kind: ErrandKind
    phase: Phase
    origin: Literal["user", "provider_cancel"] = "user"
    brief: Brief
    shortlist: Shortlist | None = None
    hold: HoldRef | None = None
    approval_id: str | None = None  # the approvals table is authoritative (M4)
    intent_id: str | None = None  # the action_intents table is authoritative (M5)
    replaces_booking_id: str | None = None
    rebook: RebookInfo | None = None
