"""prepare_proposal for a cancel (architecture 8.6, 9.5, N1). Code only, no model call.

1. Validate again against a fresh, code-issued `list_bookings` (the planner's check can
   be a turn old): the booking is the user's, `confirmed`, upcoming, and no other cancel
   or reschedule is working on it.
2. Convert the errand in code (7.2): kind CANCEL, `target_booking_id` with provenance from
   the resolved booking, GATHERING or COMPARING to QUOTING. A booking errand that already
   has a shortlist or a card is never converted.
3. The card, its material (`CancelMaterial`: the booking, provider, slot start and status
   the fresh list showed), the fingerprint and the pending approval, inserted with every
   older live row superseded. There is no hold: the click approves this booking
   being cancelled, nothing else.
4. AWAITING_APPROVAL; `respond` posts the card and parks.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from errand.agent import cards
from errand.agent.changes.validate import check_cancel_proposal
from errand.agent.deps import RunDeps
from errand.agent.records import emit_swept
from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.agent.validation import Rejected
from errand.approvals import service
from errand.approvals.fingerprint import fingerprint
from errand.clock import from_ms, to_ms
from errand.domain.brief import ErrandKind, Provenance, TextFact
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UserMessage
from errand.domain.material import CancelMaterial, ist_seconds
from errand.domain.outcomes import CardReady, Proposal, ProposalRefused, StepResult
from errand.store.repos.approvals import ApprovalRow
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import ListBookingsOut

REFUSED_TEXT = {
    "not_convertible": "I'm already working on another request here, so I didn't prepare a "
    "cancellation. Finish or decline that first.",
    "unknown_booking": "I can't find that booking any more, so I didn't prepare a cancellation.",
    "not_confirmed": "That booking is not active any more, so there is nothing to cancel.",
    "already_started": "That booking has already started, so it can't be cancelled.",
    "change_in_progress": "A change to that booking is already in progress.",
    "unavailable": "I couldn't check your bookings just now, so I didn't prepare anything. "
    "Nothing was cancelled.",
}


async def prepare_cancel(
    deps: RunDeps,
    errand: Errand,
    *,
    booking_id: str,
    proposal_id: str,
    approval_id: str,
    turn: int,
) -> tuple[Errand, StepResult]:
    try:
        listed = await deps.tools.call(Caller.PROPOSER, "list_bookings", {"user_id": deps.user_id})
    except ToolError:
        return errand, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    if not isinstance(listed, ListBookingsOut):
        return errand, ProposalRefused(reason="unavailable", text=REFUSED_TEXT["unavailable"])
    async with deps.db.session() as repos:
        live = await repos.intents.live_targets(deps.user_id)
    verdict = check_cancel_proposal(
        errand, booking_id, listed.bookings, now=deps.clock.now(), live_targets=live
    )
    if isinstance(verdict, Rejected):
        deps.tracer.emit(
            "guard.planner_tool_rejected",
            detail={"tool": "prepare_proposal", "reason": verdict.code},
        )
        return errand, ProposalRefused(
            reason=verdict.code, text=REFUSED_TEXT.get(verdict.code, verdict.message)
        )
    booking = verdict.booking
    provenance = Provenance(source="booking_history", booking_id=booking.booking_id)
    brief = errand.brief.model_copy(
        update={
            "target_booking_id": TextFact(
                value=booking.booking_id, prov=provenance, set_at_turn=turn
            ),
            "version": errand.brief.version + 1,
        }
    )
    errand = errand.model_copy(update={"kind": ErrandKind.CANCEL, "brief": brief})
    errand = transition(errand, Phase.QUOTING, "proposal", deps.tracer)
    material = CancelMaterial(
        user_id=deps.user_id,
        booking_id=booking.booking_id,
        provider_id=booking.provider_id,
        slot_start=ist_seconds(booking.slot.start),
    )
    fp = fingerprint(material)
    now = deps.clock.now()
    expires = now + timedelta(milliseconds=deps.approval_policy.ttl_ms)
    card = cards.cancel_card(
        approval_id=approval_id,
        errand_id=errand.errand_id,
        fingerprint=fp,
        material=material,
        booking=booking,
        expires_at=expires,
        tz=deps.tz,
    )
    row = ApprovalRow(
        approval_id=approval_id,
        conversation_id=deps.conversation_id,
        errand_id=errand.errand_id,
        proposal_id=proposal_id,
        action="cancel",
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


async def run_node(deps: RunDeps, state: ThreadState) -> dict[str, Any]:
    """The `prepare_proposal` node for a cancel proposal (dispatched from the node)."""
    from errand.agent.nodes.prepare_proposal import ids_for

    assert state.errand is not None and isinstance(state.plan_result, Proposal)
    plan = state.plan_result
    assert plan.booking_id is not None
    inbound = state.inbound
    turn = inbound.turn_no if isinstance(inbound, UserMessage) else 0
    proposal_id, approval_id = ids_for(state.run_id, state.counters.replans)
    with deps.tracer.node("prepare_proposal", phase_before=state.errand.phase.value) as span:
        errand, result = await prepare_cancel(
            deps,
            state.errand,
            booking_id=plan.booking_id,
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
