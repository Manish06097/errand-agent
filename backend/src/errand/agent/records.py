"""Reads of the agent's own records that nodes share (M4).

Everything here reads agent.sqlite and renders plain facts; none of it calls the world
or the model. Every "is there a live approval" read goes through the service's lazy
sweep first, and what the sweep ended is traced (`approval.expired`, `card.update`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from errand.agent.claim_guard import Verified
from errand.agent.deps import RunDeps
from errand.agent.validation import OpenBooking
from errand.approvals import service
from errand.clock import from_ms, to_ms
from errand.context.builder import StateExtras
from errand.domain.windows import slot_label
from errand.store.repos.approvals import ApprovalRow
from errand.trace.tracer import Tracer
from errand.world.contracts import Slot


def emit_swept(tracer: Tracer, swept: service.Swept) -> None:
    for approval_id, left in swept:
        reason = "expired" if left == "pending" else "grant_not_executed"
        tracer.emit("approval.expired", approval_id=approval_id, reason=reason)
        tracer.emit("card.update", approval_id=approval_id, status="expired", reason=reason)


async def live_approval(deps: RunDeps) -> ApprovalRow | None:
    row, swept = await service.live_approval(
        deps.db, deps.conversation_id, to_ms(deps.clock.now()), deps.approval_policy
    )
    emit_swept(deps.tracer, swept)
    return row


def card_line(row: ApprovalRow) -> str:
    card = row.card
    state = "pending, waiting for the user" if row.status == "pending" else row.status
    return (
        f"{state}: {card['title']} ({card['provider_id']}), {card['slot_label']}, "
        f"₹{card['quoted_price_inr']:,}. Only the user approves it, on the card."
    )


async def provider_name_for(deps: RunDeps, intent_id: str | None) -> str | None:
    if intent_id is None:
        return None
    async with deps.db.session() as repos:
        intent = await repos.intents.get(intent_id)
        approval = await repos.approvals.get(intent.approval_id) if intent else None
    if approval is None:
        return None
    name = approval.card.get("provider_name")
    return str(name) if name else None


async def state_extras(deps: RunDeps, notes: Sequence[str]) -> StateExtras:
    async with deps.db.session() as repos:
        last = await repos.known_bookings.latest_for_conversation(deps.conversation_id)
    last_line = None
    if last is not None:
        name = await provider_name_for(deps, last.source_intent_id) or last.provider_id
        slot = Slot(start=from_ms(last.slot_start), end=from_ms(last.slot_end))
        verified = "confirmed with the provider" if last.status == "confirmed" else last.status
        last_line = (
            f"{last.booking_id}, {name} ({last.provider_id}), {last.service}, "
            f"{slot_label(slot, deps.tz)}, {verified}"
        )
    row = await live_approval(deps)
    return StateExtras(
        last_booking=last_line,
        approval_card=card_line(row) if row is not None else None,
        notes=tuple(notes),
    )


async def open_bookings(deps: RunDeps) -> list[OpenBooking]:
    """Provider and slot of every unresolved book intent of this user, from its material."""
    async with deps.db.session() as repos:
        intents = await repos.intents.unresolved_for_user(deps.user_id)
    out: list[OpenBooking] = []
    for intent in intents:
        if intent.action != "book":
            continue
        material = json.loads(intent.material_json)
        out.append(
            OpenBooking(
                provider_id=str(material["provider_id"]),
                slot_start=datetime.fromisoformat(str(material["slot_start"])),
            )
        )
    return out


async def verified_records(deps: RunDeps, this_run: set[str]) -> list[Verified]:
    """What can back a claim: bookings verified in this run and known_bookings."""
    async with deps.db.session() as repos:
        known = await repos.known_bookings.for_user(deps.user_id)
    out: list[Verified] = []
    for b in known:
        if b.status != "confirmed":
            continue
        name = await provider_name_for(deps, b.source_intent_id) or b.provider_id
        out.append(
            Verified(
                booking_id=b.booking_id,
                provider_id=b.provider_id,
                provider_name=name,
                service=b.service,
                slot_start=from_ms(b.slot_start),
                kind="book",
                this_run=b.booking_id in this_run,
            )
        )
    return out
