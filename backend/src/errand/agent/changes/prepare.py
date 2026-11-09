"""prepare_proposal for a cancel or a reschedule (architecture 8.6, 9.5, N1). Code only,
no model call.

1. Validate again against a fresh, code-issued `list_bookings` (the planner's check can be
   a turn old): the booking is the user's, `confirmed`, upcoming, and no other cancel or
   reschedule is working on it.
2. Convert the errand in code (7.2): kind CANCEL or RESCHEDULE, `target_booking_id` with
   provenance from the resolved booking, GATHERING or COMPARING to QUOTING. A booking
   errand that already has a shortlist or a card is never converted.
3. A reschedule builds its candidates from the booked provider's own slots
   (`get_provider(booking.provider_id).available_slots`), filtered by the brief's window
   and by `slot_filter` (N2's memory filters plug in there; it defaults to none, and no
   memory is read here), minus the booking's own slot. The card proposes the eligible slot
   on the earliest day closest to the original time of day; the other eligible slots
   ride on the card as `select_slot` buttons that card that slot instead.
4. The card, its material (`CancelMaterial` or `RescheduleMaterial`), the fingerprint and
   the pending approval, inserted with every older live row superseded. There is no hold
   and no price: the click approves this one change.
5. AWAITING_APPROVAL; `respond` posts the card and parks.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from errand.agent import cards
from errand.agent.changes.validate import ValidCancel, check_change_proposal
from errand.agent.deps import RunDeps
from errand.agent.records import emit_swept
from errand.agent.state import ThreadState
from errand.agent.transitions import INITIAL_PHASE, transition
from errand.agent.validation import Rejected
from errand.approvals import service
from errand.approvals.fingerprint import fingerprint
from errand.clock import from_ms, to_ms
from errand.domain.brief import Brief, ErrandKind, Provenance, TextFact
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UiAction, UserMessage
from errand.domain.material import CancelMaterial, RescheduleMaterial, ist_seconds
from errand.domain.messages import SlotOption
from errand.domain.outcomes import CardReady, Proposal, ProposalRefused, StepResult
from errand.domain.slots import Candidate, Exclusion, Shortlist, SlotHandle
from errand.domain.windows import slot_fits, slot_label
from errand.ids import errand_id_for
from errand.store.repos.approvals import ApprovalRow
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import (
    BookingItem,
    GetProviderOut,
    ListBookingsOut,
    Slot,
)

REFUSED_TEXT = {
    "not_convertible": "I'm already working on another request here, so I didn't prepare a "
    "change. Finish or decline that first.",
    "unknown_booking": "I can't find that booking any more, so I didn't prepare a change.",
    "not_confirmed": "That booking is not active any more, so there is nothing to change.",
    "already_started": "That booking has already started, so it can't be changed.",
    "change_in_progress": "A change to that booking is already in progress.",
    "unavailable": "I couldn't check your bookings just now, so I didn't prepare anything. "
    "Nothing was changed.",
    "no_window": "I don't have the new day yet. Tell me which day you want it moved to.",
    "unknown_slot": "That time isn't on offer any more, so I didn't prepare a move.",
}
# A reason a slot is not offered (None: it is). Defaults to offering every slot; N2's
# earliest_time, latest_time and avoid_days plug in here.
SlotFilter = Callable[[Slot], str | None]


def no_filter(slot: Slot) -> str | None:
    return None


def handles(state: ThreadState) -> bool:
    """Is this `prepare_proposal` input a cancel or a reschedule (dispatched from the node)?"""
    plan = state.plan_result
    if isinstance(plan, Proposal) and plan.action != "book":
        return True
    errand = state.errand
    return (
        errand is not None
        and errand.kind is ErrandKind.RESCHEDULE
        and isinstance(state.inbound, UiAction)
        and state.inbound.kind == "select_slot"
    )


async def run_node(deps: RunDeps, state: ThreadState) -> dict[str, Any]:
    """The `prepare_proposal` node for a cancel or reschedule input."""
    from errand.agent.nodes.prepare_proposal import ids_for

    assert state.errand is not None
    plan = state.plan_result
    inbound = state.inbound
    turn = inbound.turn_no if isinstance(inbound, UserMessage) else 0
    proposal_id, approval_id = ids_for(state.run_id, state.counters.replans)
    chosen: str | None = None
    action: str
    booking_id: str | None
    if isinstance(plan, Proposal):
        action, booking_id = plan.action, plan.booking_id
    else:
        # A select_slot click on a reschedule card or on the alternatives of a conflict.
        assert isinstance(inbound, UiAction)
        action, chosen = "reschedule", inbound.payload.get("slot_ref", "")
        target = state.errand.brief.target_booking_id
        booking_id = target.value if target is not None else None
    errand: Errand
    result: StepResult
    with deps.tracer.node("prepare_proposal", phase_before=state.errand.phase.value) as span:
        if booking_id is None:
            errand = state.errand
            result = ProposalRefused(reason="unknown_slot", text=REFUSED_TEXT["unknown_slot"])
        elif action == "cancel":
            errand, result = await prepare_cancel(
                deps,
                state.errand,
                booking_id=booking_id,
                proposal_id=proposal_id,
                approval_id=approval_id,
                turn=turn,
            )
        else:
            errand, result = await prepare_reschedule(
                deps,
                state.errand,
                booking_id=booking_id,
                chosen_ref=chosen,
                proposal_id=proposal_id,
                approval_id=approval_id,
                turn=turn,
            )
        span.end_fields["phase_after"] = errand.phase.value
    return {
        "errand": errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }


# ------------------------------------------------------------------ shared steps


async def _target(
    deps: RunDeps, errand: Errand, booking_id: str
) -> tuple[ValidCancel, None] | tuple[None, ProposalRefused]:
    try:
        listed = await deps.tools.call(Caller.PROPOSER, "list_bookings", {"user_id": deps.user_id})
    except ToolError:
        return None, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    if not isinstance(listed, ListBookingsOut):
        return None, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    async with deps.db.session() as repos:
        live = await repos.intents.live_targets(deps.user_id)
        waiting = await repos.intents.awaiting_provider(deps.conversation_id)
    verdict = check_change_proposal(
        errand,
        booking_id,
        listed.bookings,
        now=deps.clock.now(),
        live_targets=live,
        awaiting_provider=waiting,
    )
    if isinstance(verdict, Rejected):
        deps.tracer.emit(
            "guard.planner_tool_rejected",
            detail={"tool": "prepare_proposal", "reason": verdict.code},
        )
        return None, ProposalRefused(
            reason=verdict.code, text=REFUSED_TEXT.get(verdict.code, verdict.message)
        )
    return verdict, None


def _convert(
    deps: RunDeps, errand: Errand, kind: ErrandKind, booking: BookingItem, turn: int
) -> Errand:
    """The errand becomes a cancel or reschedule, in code (7.2), with the target and its
    provenance. An errand in NEEDS_ATTENTION whose intent nothing waits on (S65) ends, and
    the change is a new errand; a reschedule keeps the new day the user gave."""
    window = errand.brief.window
    if errand.phase is Phase.NEEDS_ATTENTION:
        errand = transition(errand, Phase.ABANDONED, "superseded_by_change", deps.tracer)
        errand = Errand(
            errand_id=errand_id_for(deps.run_id),
            kind=kind,
            phase=INITIAL_PHASE,
            brief=Brief(window=window if kind is ErrandKind.RESCHEDULE else None),
        )
        deps.tracer.errand_id = errand.errand_id
    provenance = Provenance(source="booking_history", booking_id=booking.booking_id)
    brief = errand.brief.model_copy(
        update={
            "target_booking_id": TextFact(
                value=booking.booking_id, prov=provenance, set_at_turn=turn
            ),
            "version": errand.brief.version + 1,
        }
    )
    errand = errand.model_copy(update={"kind": kind, "brief": brief})
    return transition(errand, Phase.QUOTING, "proposal", deps.tracer)


async def _park(
    deps: RunDeps,
    errand: Errand,
    *,
    action: str,
    material: CancelMaterial | RescheduleMaterial,
    card: Any,
    proposal_id: str,
    approval_id: str,
    expires: datetime,
) -> tuple[Errand, StepResult]:
    """Insert the pending approval (superseding older live rows) and show the card."""
    fp = fingerprint(material)
    now = deps.clock.now()
    row = ApprovalRow(
        approval_id=approval_id,
        conversation_id=deps.conversation_id,
        errand_id=errand.errand_id,
        proposal_id=proposal_id,
        action=action,
        fingerprint=fp,
        material_json=material.model_dump_json(),
        card_json=card.model_dump_json(),
        bound_hold_id=None,
        status="pending",
        granted_via=None,
        granted_message_id=None,
        invalidated_reason=None,
        consumed_by_intent=None,
        requested_at=to_ms(now),
        granted_at=None,
        consumed_at=None,
        expires_at=to_ms(expires),
    )
    created = await service.create(deps.db, row, deps.approval_policy)
    emit_swept(deps.tracer, created.swept)
    for old in created.superseded:
        deps.tracer.emit("approval.invalidated", approval_id=old, reason="superseded", new_fp=fp)
        deps.tracer.emit("card.update", approval_id=old, status="invalidated", reason="superseded")
    if created.created:
        deps.tracer.emit(
            "approval.requested",
            approval_id=approval_id,
            fingerprint=fp,
            hold_id=None,
            expires_at=from_ms(row.expires_at).isoformat(),
            superseded=created.superseded,
        )
    errand = transition(errand, Phase.AWAITING_APPROVAL, "card_shown", deps.tracer)
    errand = errand.model_copy(update={"approval_id": approval_id, "hold": None})
    return errand, CardReady(approval_id=approval_id)


# ------------------------------------------------------------------ cancel


async def prepare_cancel(
    deps: RunDeps,
    errand: Errand,
    *,
    booking_id: str,
    proposal_id: str,
    approval_id: str,
    turn: int,
) -> tuple[Errand, StepResult]:
    valid, refused = await _target(deps, errand, booking_id)
    if valid is None:
        assert refused is not None
        return errand, refused
    booking = valid.booking
    errand = _convert(deps, errand, ErrandKind.CANCEL, booking, turn)
    material = CancelMaterial(
        user_id=deps.user_id,
        booking_id=booking.booking_id,
        provider_id=booking.provider_id,
        slot_start=ist_seconds(booking.slot.start),
    )
    expires = deps.clock.now() + timedelta(milliseconds=deps.approval_policy.ttl_ms)
    card = cards.cancel_card(
        approval_id=approval_id,
        errand_id=errand.errand_id,
        fingerprint=fingerprint(material),
        material=material,
        booking=booking,
        expires_at=expires,
        tz=deps.tz,
    )
    return await _park(
        deps,
        errand,
        action="cancel",
        material=material,
        card=card,
        proposal_id=proposal_id,
        approval_id=approval_id,
        expires=expires,
    )


# ------------------------------------------------------------------ reschedule


def conflict_key(provider_id: str, start: datetime) -> str:
    """How a slot the provider refused (`slot_conflict`) is remembered in the shortlist's
    exclusions, so it is not offered again."""
    return f"{provider_id}@{start.isoformat()}"


def eligible_slots(
    provider: GetProviderOut,
    booking: BookingItem,
    errand: Errand,
    *,
    now: datetime,
    deps: RunDeps,
    slot_filter: SlotFilter,
) -> list[Slot]:
    """The provider's slots that fit the new window, minus the booking's own and any the
    provider already refused for this move, sorted."""
    window = errand.brief.window
    if window is None:
        return []
    refused = {
        e.provider_id
        for e in (errand.shortlist.excluded if errand.shortlist else [])
        if e.reason == "slot_conflict"
    }
    fits = [
        s
        for s in provider.available_slots
        if s.start > now
        and s.start != booking.slot.start
        and conflict_key(provider.provider_id, s.start) not in refused
        and slot_fits(s, window.value, deps.tz)
        and slot_filter(s) is None
    ]
    return sorted(fits, key=lambda s: s.start)


def closest_to(slots: list[Slot], original: datetime, tz: Any) -> Slot:
    """Of the eligible slots on the earliest day that has one, the one closest to the
    booking's original time of day (the earlier on a tie)."""
    first_day = slots[0].start.astimezone(tz).date()
    same_day = [s for s in slots if s.start.astimezone(tz).date() == first_day]
    target = original.astimezone(tz)
    minutes = target.hour * 60 + target.minute

    def distance(slot: Slot) -> tuple[int, datetime]:
        local = slot.start.astimezone(tz)
        return abs(local.hour * 60 + local.minute - minutes), slot.start

    return min(same_day, key=distance)


def shortlist_of(
    provider: GetProviderOut,
    slots: list[Slot],
    errand: Errand,
    label: str,
    excluded: list[Exclusion] | None = None,
) -> Shortlist:
    """The provider's eligible slots as handles (S1, ...), so a select_slot click can name
    one. Stored on the errand; the same click path as a booking's."""
    handles = [
        SlotHandle(
            ref=f"S{i}",
            provider_id=provider.provider_id,
            slot=slot,
            search_price_inr=provider.price_inr,
        )
        for i, slot in enumerate(slots, start=1)
    ]
    candidate = Candidate(
        provider_id=provider.provider_id,
        name=provider.name,
        rating=provider.rating,
        price_inr=provider.price_inr,
        review_count=len(provider.reviews),
        verified=provider.attributes.verified,
        slots=handles,
    )
    return Shortlist(brief_version=errand.brief.version, window_label=label, candidates=[candidate])


async def prepare_reschedule(
    deps: RunDeps,
    errand: Errand,
    *,
    booking_id: str,
    chosen_ref: str | None,
    proposal_id: str,
    approval_id: str,
    turn: int,
    slot_filter: SlotFilter = no_filter,
) -> tuple[Errand, StepResult]:
    if errand.brief.window is None:
        return errand, ProposalRefused(reason="no_window", text=REFUSED_TEXT["no_window"])
    valid, refused = await _target(deps, errand, booking_id)
    if valid is None:
        assert refused is not None
        return errand, refused
    booking = valid.booking
    try:
        found = await deps.tools.call(
            Caller.PROPOSER, "get_provider", {"provider_id": booking.provider_id}
        )
    except ToolError:
        return errand, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    if not isinstance(found, GetProviderOut):
        return errand, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    wanted_start: datetime | None = None
    if chosen_ref:
        handle = errand.shortlist.handle(chosen_ref) if errand.shortlist is not None else None
        if handle is None or handle.provider_id != booking.provider_id:
            return errand, ProposalRefused(reason="unknown_slot", text=REFUSED_TEXT["unknown_slot"])
        wanted_start = handle.slot.start
    errand = _convert(deps, errand, ErrandKind.RESCHEDULE, booking, turn)
    window = errand.brief.window
    assert window is not None
    slots = eligible_slots(
        found, booking, errand, now=deps.clock.now(), deps=deps, slot_filter=slot_filter
    )
    if not slots:
        errand = transition(errand, Phase.COMPARING, "no_eligible_slot", deps.tracer)
        text = (
            f"{booking.provider_name} has no free time in {window.value.label}, so I didn't "
            "prepare a move. Nothing was changed. Another day?"
        )
        return errand, ProposalRefused(reason="no_slot", text=text)
    kept = list(errand.shortlist.excluded) if errand.shortlist is not None else []
    errand = errand.model_copy(
        update={"shortlist": shortlist_of(found, slots, errand, window.value.label, kept)}
    )
    if wanted_start is not None:
        picked = next((s for s in slots if s.start == wanted_start), None)
        if picked is None:
            errand = transition(errand, Phase.COMPARING, "slot_gone", deps.tracer)
            return errand, ProposalRefused(reason="unknown_slot", text=REFUSED_TEXT["unknown_slot"])
    else:
        picked = closest_to(slots, booking.slot.start, deps.tz)
    refs = {h.slot.start: h.ref for h in errand.shortlist.candidates[0].slots}  # type: ignore[union-attr]
    alternatives = [
        SlotOption(
            ref=refs[s.start],
            start=s.start.isoformat(),
            end=s.end.isoformat(),
            label=slot_label(s, deps.tz),
        )
        for s in slots
        if s.start != picked.start
    ]
    material = RescheduleMaterial(
        user_id=deps.user_id,
        booking_id=booking.booking_id,
        provider_id=booking.provider_id,
        from_start=ist_seconds(booking.slot.start),
        to_start=ist_seconds(picked.start),
        to_end=ist_seconds(picked.end),
    )
    expires = deps.clock.now() + timedelta(milliseconds=deps.approval_policy.ttl_ms)
    card = cards.reschedule_card(
        approval_id=approval_id,
        errand_id=errand.errand_id,
        fingerprint=fingerprint(material),
        material=material,
        booking=booking,
        new_slot=picked,
        alternatives=alternatives,
        expires_at=expires,
        tz=deps.tz,
    )
    return await _park(
        deps,
        errand,
        action="reschedule",
        material=material,
        card=card,
        proposal_id=proposal_id,
        approval_id=approval_id,
        expires=expires,
    )
