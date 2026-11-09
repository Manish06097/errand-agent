"""What a follow-up run carries between its nodes (architecture 14, Gate 3).

The followup node decides and searches; `respond` renders from this context and writes the
messages, the notification and the job move in one place. Everything in it is a fact from
records (the booking, the policy decision, the search), never model text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AltSlot(_Model):
    """A next-best candidate as a slot button: a tap is `select_slot`, which holds it."""

    slot_ref: str
    label: str  # "Fri 14 Nov, 10:00-11:00 with Arjun Plumbing, ₹1,200"


class WidenOption(_Model):
    field: Literal["days", "price", "time"]
    label: str
    value: str | None = None  # the rupee ceiling of an "Up to ₹N" button


Variant = Literal[
    "replacement",  # a Rebook card follows
    "nothing_fits",  # searched, held nothing, three buttons
    "no_search",  # another booking of the service is already in the window
    "nothing_to_cancel",  # a cancel card was open: no offer
    "ask_area",  # no area known: "Where should I look?"
    "dismissed",
    "lapsed",
    "stale",  # the job was not (or no longer) in the state this run needs
]


class FollowupCtx(_Model):
    job_id: str
    booking_id: str
    mode: Literal["start", "widen", "dismiss", "lapse"] = "start"
    variant: Variant = "stale"
    urgent: bool = False
    provider_name: str = ""
    service: str = ""
    slot_label: str = ""  # the cancelled slot, "Thu 13 Nov at 09:00" or "tomorrow at 09:00"
    searched: str | None = None  # "Thu 12 Nov to Sat 15 Nov, mornings, up to ₹1,500"
    # The booking that made a search pointless: provider, slot ("Fri 14 Nov at 10:00"), id.
    existing: tuple[str, str, str] | None = None
    held_until: str | None = None
    alternatives: list[AltSlot] = Field(default_factory=list)
    widen: list[WidenOption] = Field(default_factory=list)
    # Whether a replacement is ready, for the bell title; None when nothing was searched.
    ready: bool | None = None
