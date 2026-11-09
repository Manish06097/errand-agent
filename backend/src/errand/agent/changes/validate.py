"""Pure checks for a cancel proposal (architecture 8.6, 9.5, N1).

`check_cancel_proposal` is the one entry point whoever makes the proposal: the planner's
`propose_cancel` (a rejection is a tool error the model can correct) and
`prepare_proposal`, which runs it again against a fresh list before it builds the card.
`Rejected.code` values are stable:

- `not_convertible`: the errand is a booking that already has a shortlist or a card (or a
  change with a card pending); finish or decline that first;
- `unknown_booking`: not one of the user's bookings in the fresh list;
- `not_confirmed`: the booking is not `confirmed` (completed, cancelled, cancelled by the
  provider);
- `already_started`: the booking's slot has started;
- `change_in_progress`: another cancel or reschedule is still working on that booking.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from errand.agent.validation import Rejected
from errand.domain.brief import ErrandKind
from errand.domain.errand import Errand, Phase
from errand.world.contracts import BookingItem


@dataclass(frozen=True)
class ValidCancel:
    booking: BookingItem


def convertible(errand: Errand, *, awaiting_provider: bool = False) -> Rejected | None:
    """May this errand become a cancel or reschedule (7.2: GATHERING or COMPARING to
    QUOTING)? A booking with a shortlist or a card is never converted.

    An errand in NEEDS_ATTENTION is not converted either, but it does not block: when its
    intent is not waiting on the provider (the provider recorded something other than what
    was approved, S65) the cancel starts as a new errand and the old one is abandoned
    (`awaiting_provider` False). While the provider's answer is still pending, the booking
    may yet appear, so the user waits for it."""
    if errand.phase is Phase.NEEDS_ATTENTION:
        if awaiting_provider:
            return Rejected(
                "not_convertible",
                "a booking is still being confirmed with the provider; wait for its result",
            )
        return None
    if errand.kind is ErrandKind.REBOOK:
        return Rejected("not_convertible", "finish the rebooking before changing a booking")
    if errand.kind is ErrandKind.BOOK:
        if errand.phase is Phase.GATHERING and errand.shortlist is None and not errand.approval_id:
            return None
        return Rejected(
            "not_convertible",
            "a booking request is already in progress; finish it or have the user decline "
            "it before cancelling or moving another booking",
        )
    if errand.phase in (Phase.GATHERING, Phase.COMPARING):
        return None
    return Rejected("not_convertible", "a change to a booking is in progress; wait for its result")


def check_cancel_proposal(
    errand: Errand,
    booking_id: str,
    bookings: Sequence[BookingItem],
    *,
    now: datetime,
    live_targets: Collection[str],
    awaiting_provider: bool = False,
) -> ValidCancel | Rejected:
    """`bookings` is a fresh code-issued list_bookings answer."""
    refused = convertible(errand, awaiting_provider=awaiting_provider)
    if refused is not None:
        return refused
    booking = next((b for b in bookings if b.booking_id == booking_id), None)
    if booking is None:
        return Rejected("unknown_booking", f"{booking_id} is not one of the user's bookings")
    if booking.status != "confirmed":
        return Rejected(
            "not_confirmed", f"that booking is {booking.status}; it cannot be cancelled"
        )
    if booking.slot.start <= now:
        return Rejected("already_started", "that booking has already started")
    if booking_id in live_targets:
        return Rejected("change_in_progress", "a change to that booking is already in progress")
    return ValidCancel(booking)
