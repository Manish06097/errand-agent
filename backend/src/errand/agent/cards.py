"""Code-rendered approval cards, reminders, outcome cards and status lines (M4).

Every word on these comes from records: the held quote, the slot, the provider name
from the shortlist, the user's nearby bookings from a fresh `list_bookings`. Model text
never reaches a card.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.agent.brief_ops import render_value
from errand.approvals.confirm_grammar import suggestion
from errand.domain.brief import Brief, BriefField
from errand.domain.errand import Phase
from errand.domain.material import BookMaterial
from errand.domain.messages import (
    ActionCard,
    BriefChip,
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


def status_line(phase: Phase) -> str | None:
    return STATUS_LINES.get(phase)


_CHIP_EVIDENCE_CHARS = 60
_WINDOW_FIELDS = (BriefField.WINDOW, BriefField.NEW_WINDOW)


def brief_chips(brief: Brief) -> list[BriefChip]:
    """Every filled field of the brief as a chip, in field order (M8)."""
    chips: list[BriefChip] = []
    for name in BriefField:
        fact = getattr(brief, name.value)
        if fact is None:
            continue
        evidence = fact.prov.evidence
        if evidence is not None and len(evidence) > _CHIP_EVIDENCE_CHARS:
            evidence = evidence[: _CHIP_EVIDENCE_CHARS - 3] + "..."
        chips.append(
            BriefChip(
                field=name,
                value=render_value(fact),
                source=fact.prov.source,
                evidence=evidence,
                window_label=fact.value.label
                if name in (BriefField.WINDOW, BriefField.NEW_WINDOW)
                else None,
            )
        )
    return chips


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
    brief: Brief | None = None,
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
        brief=brief_chips(brief) if brief is not None else [],
    )


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
        ),
        button_label=card.button_label,
        typed_grant_ok=typed_grant_ok,
        hint=hint,
        status_line=STATUS_LINES[Phase.AWAITING_APPROVAL],
    )


def confirm_hint(card: ActionCard) -> str:
    return f"To confirm, tap '{card.button_label}' or type '{suggestion(card.action)}'."


def booked_card(card: ActionCard, booking_id: str) -> OutcomeCard:
    return OutcomeCard(
        outcome=RunOutcome.BOOKED_VERIFIED,
        verified=True,
        title=f"Booked: {card.provider_name}",
        text=(
            f"Booked {card.provider_name} for {card.slot_label} at "
            f"{rupees(card.quoted_price_inr)} ({booking_id}). Confirmed with the provider."
        ),
        lines=[
            MaterialLine(label="Booking", value=booking_id),
            MaterialLine(label="When", value=card.slot_label),
            MaterialLine(label="Price", value=rupees(card.quoted_price_inr)),
        ],
        booking_id=booking_id,
        approval_id=card.approval_id,
    )
