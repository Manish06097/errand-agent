"""Code-rendered approval cards, reminders, outcome cards and status lines (M4).

Every word on these comes from records: the held quote, the slot, the provider name
from the shortlist, the user's nearby bookings from a fresh `list_bookings`. Model text
never reaches a card.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.approvals.confirm_grammar import suggestion
from errand.domain.brief import ErrandKind
from errand.domain.errand import Phase
from errand.domain.material import BookMaterial, CancelMaterial
from errand.domain.messages import (
    ActionCard,
    CardReminder,
    MaterialLine,
    NearbyBooking,
    OutcomeCard,
)
from errand.domain.outcomes import RunOutcome
from errand.domain.slots import Candidate
from errand.domain.windows import slot_label
from errand.world.contracts import BookingItem, Slot

STATUS_LINES: dict[Phase, str] = {
    Phase.GATHERING: "Nothing booked for this request yet.",
    Phase.COMPARING: "Nothing booked for this request yet.",
    Phase.QUOTING: "Not booked yet: getting a quote.",
    Phase.AWAITING_APPROVAL: "Not booked yet: waiting for your approval.",
    Phase.EXECUTING: "Checking with the provider.",
    Phase.VERIFYING: "Checking with the provider.",
    Phase.NEEDS_ATTENTION: "Not confirmed yet: still checking with the provider.",
    Phase.FAILED: "Not booked.",
    Phase.ABANDONED: "Nothing was booked.",
}


# What "not done yet" reads for a cancel or a reschedule, so a cancel card never says
# "Not booked" (N1).
CHANGE_LINES: dict[ErrandKind, dict[Phase, str]] = {
    ErrandKind.CANCEL: {
        Phase.GATHERING: "Nothing cancelled yet.",
        Phase.COMPARING: "Nothing cancelled yet.",
        Phase.QUOTING: "Not cancelled yet: preparing the card.",
        Phase.AWAITING_APPROVAL: "Not cancelled yet: waiting for your approval.",
        Phase.FAILED: "Not cancelled.",
        Phase.ABANDONED: "Nothing was cancelled.",
    },
    ErrandKind.RESCHEDULE: {
        Phase.GATHERING: "Nothing moved yet.",
        Phase.COMPARING: "Nothing moved yet.",
        Phase.QUOTING: "Not moved yet: preparing the card.",
        Phase.AWAITING_APPROVAL: "Not moved yet: waiting for your approval.",
        Phase.FAILED: "Not moved.",
        Phase.ABANDONED: "Nothing was moved.",
    },
}


def status_line(phase: Phase, kind: ErrandKind = ErrandKind.BOOK) -> str | None:
    return CHANGE_LINES.get(kind, {}).get(phase) or STATUS_LINES.get(phase)


def rupees(amount: int) -> str:
    return f"₹{amount:,}"


def service_words(service: str) -> str:
    return {"ac_servicing": "AC servicing"}.get(service, service.replace("_", " ").capitalize())


def button_label(quote: int) -> str:
    return f"Book for {rupees(quote)}"


def book_card(
    *,
    approval_id: str,
    errand_id: str,
    fingerprint: str,
    material: BookMaterial,
    candidate: Candidate,
    slot: Slot,
    search_price_inr: int,
    hold_expires_at: datetime,
    expires_at: datetime,
    nearby: list[BookingItem],
    tz: tzinfo,
) -> ActionCard:
    quote = material.quoted_price_inr
    delta = quote - search_price_inr
    label = slot_label(slot, tz)
    if delta:
        sign = "+" if delta > 0 else "-"
        price = (
            f"{rupees(quote)} (search price {rupees(search_price_inr)}, {sign}{rupees(abs(delta))})"
        )
    else:
        price = f"{rupees(quote)} (same as the search price)"
    lines = [
        MaterialLine(label="Provider", value=candidate.name),
        MaterialLine(label="Service", value=service_words(material.service.value)),
        MaterialLine(label="When", value=label),
        MaterialLine(label="Price", value=price),
    ]
    near = [
        NearbyBooking(
            booking_id=b.booking_id, provider_name=b.provider_name, label=slot_label(b.slot, tz)
        )
        for b in nearby
    ]
    if near:
        what = "; ".join(f"{n.provider_name}, {n.label} ({n.booking_id})" for n in near)
        lines.append(MaterialLine(label="Already booked", value=f"You already have {what}"))
    return ActionCard(
        approval_id=approval_id,
        errand_id=errand_id,
        action="book",
        fingerprint=fingerprint,
        title=f"Book {candidate.name}",
        lines=lines,
        provider_id=candidate.provider_id,
        provider_name=candidate.name,
        service=material.service.value,
        slot_start=slot.start.isoformat(),
        slot_end=slot.end.isoformat(),
        slot_label=label,
        search_price_inr=search_price_inr,
        quoted_price_inr=quote,
        price_delta_inr=delta,
        hold_expires_at=hold_expires_at.isoformat(),
        expires_at=expires_at.isoformat(),
        button_label=button_label(quote),
        nearby=near,
        material=material,
        status_line=STATUS_LINES[Phase.AWAITING_APPROVAL],
    )


def _approval_line(action: str) -> str:
    kind = ErrandKind(action) if action != "book" else ErrandKind.BOOK
    return status_line(Phase.AWAITING_APPROVAL, kind) or STATUS_LINES[Phase.AWAITING_APPROVAL]


def reminder(card: ActionCard, *, typed_grant_ok: bool) -> CardReminder:
    phrase = suggestion(card.action)
    hint = (
        f"Tap '{card.button_label}' or type '{phrase}' to confirm."
        if typed_grant_ok
        else "Tap the button to confirm."
    )
    return CardReminder(
        approval_id=card.approval_id,
        fingerprint=card.fingerprint,
        text=(
            f"Still waiting on: {card.title}, {card.slot_label}, {rupees(card.quoted_price_inr)}"
            if card.action == "book"
            else f"Still waiting on: {card.title}, {card.slot_label}"
        ),
        button_label=card.button_label,
        typed_grant_ok=typed_grant_ok,
        hint=hint,
        status_line=_approval_line(card.action),
    )


def confirm_hint(card: ActionCard) -> str:
    return f"To confirm, tap '{card.button_label}' or type '{suggestion(card.action)}'."


def booked_card(card: ActionCard, booking_id: str, *, after_restart: bool = False) -> OutcomeCard:
    suffix = " I checked after a restart." if after_restart else ""
    return OutcomeCard(
        outcome=RunOutcome.BOOKED_VERIFIED,
        verified=True,
        title=f"Booked: {card.provider_name}",
        text=(
            f"Booked {card.provider_name} for {card.slot_label} at "
            f"{rupees(card.quoted_price_inr)} ({booking_id}). Confirmed with the provider.{suffix}"
        ),
        lines=[
            MaterialLine(label="Booking", value=booking_id),
            MaterialLine(label="When", value=card.slot_label),
            MaterialLine(label="Price", value=rupees(card.quoted_price_inr)),
        ],
        booking_id=booking_id,
        approval_id=card.approval_id,
    )


# ---------------------------------------------------------------- cancel and reschedule (N1)


def cancel_card(
    *,
    approval_id: str,
    errand_id: str,
    fingerprint: str,
    material: CancelMaterial,
    booking: BookingItem,
    expires_at: datetime,
    tz: tzinfo,
) -> ActionCard:
    """The approval card for cancelling one booking. Every line comes from the fresh
    `list_bookings` record the fingerprint was computed from. There is no hold and no
    price to approve, so the price fields are zero and the card's lines say what matters;
    `hold_expires_at` repeats the approval's own expiry (nothing is held)."""
    label = slot_label(booking.slot, tz)
    lines = [
        MaterialLine(label="Provider", value=booking.provider_name),
        MaterialLine(label="Service", value=service_words(booking.service.value)),
        MaterialLine(label="When", value=label),
        MaterialLine(label="Booking", value=f"{booking.booking_id}, confirmed"),
    ]
    return ActionCard(
        approval_id=approval_id,
        errand_id=errand_id,
        action="cancel",
        fingerprint=fingerprint,
        title=f"Cancel {booking.provider_name}",
        lines=lines,
        provider_id=booking.provider_id,
        provider_name=booking.provider_name,
        service=booking.service.value,
        slot_start=booking.slot.start.isoformat(),
        slot_end=booking.slot.end.isoformat(),
        slot_label=label,
        search_price_inr=0,
        quoted_price_inr=0,
        price_delta_inr=0,
        hold_expires_at=expires_at.isoformat(),
        expires_at=expires_at.isoformat(),
        button_label="Cancel booking",
        material=material,
        status_line=CHANGE_LINES[ErrandKind.CANCEL][Phase.AWAITING_APPROVAL],
    )


def cancelled_card(card: ActionCard, booking_id: str, *, by_provider: bool = False) -> OutcomeCard:
    if by_provider:
        text = (
            f"{card.provider_name} had already cancelled {booking_id} ({card.slot_label}), "
            "so there was nothing for me to cancel."
        )
        title = f"Already cancelled by the provider: {card.provider_name}"
    else:
        text = (
            f"Cancelled {card.provider_name} for {card.slot_label} ({booking_id}). "
            "Confirmed with the provider."
        )
        title = f"Cancelled: {card.provider_name}"
    return OutcomeCard(
        outcome=RunOutcome.CANCELLED_VERIFIED,
        verified=True,
        title=title,
        text=text,
        lines=[
            MaterialLine(label="Booking", value=booking_id),
            MaterialLine(label="When", value=card.slot_label),
            MaterialLine(label="Status", value="cancelled"),
        ],
        booking_id=booking_id,
        approval_id=card.approval_id,
    )


def rescheduled_card(card: ActionCard, booking_id: str) -> OutcomeCard:
    return OutcomeCard(
        outcome=RunOutcome.RESCHEDULED_VERIFIED,
        verified=True,
        title=f"Moved: {card.provider_name}",
        text=(
            f"Moved {card.provider_name} to {card.slot_label} ({booking_id}). "
            "Confirmed with the provider."
        ),
        lines=[
            MaterialLine(label="Booking", value=booking_id),
            MaterialLine(label="New time", value=card.slot_label),
        ],
        booking_id=booking_id,
        approval_id=card.approval_id,
    )
