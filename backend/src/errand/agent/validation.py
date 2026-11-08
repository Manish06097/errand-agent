"""Proposal validation (architecture 8.6). Pure: every input is passed in.

`check_book_proposal` is the one entry point for a book proposal, whoever makes it:
the planner's `propose_booking` (a rejection is a tool error the model can correct),
a slot click (`select_slot`), and `prepare_proposal`, which runs it again before it
holds anything. `execute` repeats the duplicate part inside its guard transaction.
M10's injection scenarios (S15) and M9's cancel and reschedule land on this same
function and its `Rejected.code` values, so both are kept stable:

- `no_shortlist`, `stale_shortlist`: there is no shortlist for the current brief version;
- `bad_slot_ref`: the handle is not of the form S1, S2, ...;
- `unknown_provider`: not a candidate on the current shortlist;
- `unknown_slot`: no such handle, or it belongs to another provider;
- `slot_not_eligible`: the slot has started, or no longer fits the window or budget;
- `duplicate_booking`: the user already has a confirmed booking with that provider and
  slot, per a fresh `list_bookings` (never `known_bookings`: the world has bookings we
  did not make);
- `unresolved_intent`: a booking of the same provider and slot is still being
  confirmed with the provider.

`nearby_bookings` gives the user's confirmed bookings of the same service within a day
of the slot: they become `same_service_nearby` in the material and are shown on the card.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

from errand.domain.errand import Errand
from errand.domain.material import is_nearby
from errand.domain.slots import Candidate, SlotHandle
from errand.domain.windows import slot_fits
from errand.world.contracts import BookingItem, Service

SLOT_REF = re.compile(r"^S[1-9][0-9]{0,2}$")


@dataclass(frozen=True)
class Rejected:
    code: str
    message: str  # shown to the model as the tool error, and logged


@dataclass(frozen=True)
class Valid:
    candidate: Candidate
    handle: SlotHandle
    service: Service


@dataclass(frozen=True)
class OpenBooking:
    """An unresolved intent's provider and slot start, from its material."""

    provider_id: str
    slot_start: datetime


def check_book_proposal(
    errand: Errand,
    provider_id: str,
    slot_ref: str,
    *,
    now: datetime,
    tz: tzinfo,
    bookings: Sequence[BookingItem] | None = None,
    unresolved: Sequence[OpenBooking] = (),
) -> Valid | Rejected:
    """`bookings` is a fresh list_bookings answer; None skips the duplicate check (the
    planner loop passes one; a caller without one must run the check itself later)."""
    shortlist = errand.shortlist
    if shortlist is None:
        return Rejected("no_shortlist", "there is no shortlist yet; search first")
    if shortlist.brief_version != errand.brief.version:
        return Rejected(
            "stale_shortlist",
            "the shortlist is from an earlier version of the request; search again first",
        )
    if errand.brief.service is None:
        return Rejected("no_shortlist", "the request has no service yet")
    if not SLOT_REF.fullmatch(slot_ref):
        return Rejected("bad_slot_ref", f"{slot_ref!r} is not a slot handle such as S1")
    candidate = next((c for c in shortlist.candidates if c.provider_id == provider_id), None)
    if candidate is None:
        return Rejected("unknown_provider", f"{provider_id} is not on the current shortlist")
    handle = next((h for h in candidate.slots if h.ref == slot_ref), None)
    if handle is None:
        return Rejected("unknown_slot", f"{slot_ref} is not one of {provider_id}'s slots")
    if handle.slot.start <= now:
        return Rejected("slot_not_eligible", f"{slot_ref} has already started")
    window = errand.brief.window
    if window is not None and not slot_fits(handle.slot, window.value, tz):
        return Rejected("slot_not_eligible", f"{slot_ref} is outside the requested window")
    budget = errand.brief.budget_max_inr
    if budget is not None and isinstance(budget.value, int) and candidate.price_inr > budget.value:
        return Rejected("slot_not_eligible", f"{provider_id} is above the budget")
    for open_booking in unresolved:
        same_slot = open_booking.slot_start == handle.slot.start
        if open_booking.provider_id == provider_id and same_slot:
            return Rejected(
                "unresolved_intent",
                "that booking is still being confirmed with the provider; do not propose it",
            )
    if bookings is not None:
        dup = duplicate_of(bookings, provider_id, handle.slot.start)
        if dup is not None:
            return Rejected(
                "duplicate_booking",
                f"the user already has this booking ({dup.booking_id}); do not propose it",
            )
    return Valid(candidate=candidate, handle=handle, service=errand.brief.service.value)


def duplicate_of(
    bookings: Sequence[BookingItem], provider_id: str, slot_start: datetime
) -> BookingItem | None:
    for b in bookings:
        if b.status == "confirmed" and b.provider_id == provider_id and b.slot.start == slot_start:
            return b
    return None


def nearby_bookings(
    bookings: Sequence[BookingItem], service: Service, slot_start: datetime
) -> list[BookingItem]:
    return sorted(
        (
            b
            for b in bookings
            if b.status == "confirmed"
            and b.service == service
            and is_nearby(slot_start, b.slot.start)
        ),
        key=lambda b: b.booking_id,
    )
