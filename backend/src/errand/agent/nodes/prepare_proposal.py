"""prepare_proposal: hold, quote, card, fingerprint, pending approval (6.2, 8.6). Code only.

It runs for a validated planner proposal, a slot click (`select_slot`), a "Refresh hold"
click (`refresh_hold`) and execute's preflight sending a lapsed hold back (`Requote`).
None of these makes a model call.

1. Validate again (agent/validation.py) with a fresh code-issued `list_bookings`: the
   duplicate and nearby checks need the world's bookings, not `known_bookings`.
2. QUOTING. Reuse the `holds` row for (errand, provider, slot, generation) if its hold
   is still live with the margin; otherwise `hold_slot` as `Caller.PROPOSER` and record
   the next generation. A re-run therefore finds its own hold.
3. The held quote against the budget. Over budget means no approval row at all: the
   run goes back to `plan` once with a SystemNote, else respond asks whether to raise
   the budget (OverBudget). A failed hold does the same with `hold_failed`.
4. The card, its material and fingerprint, and the pending approval, inserted with
   every older live row superseded in the same transaction (approvals.service.create).
   The proposal and approval ids are derived from the checkpointed run id and replan
   count, never random and never from per-process state, so a re-run of this node
   (after a crash, from the checkpoint) hits `UNIQUE(proposal_id)` and reuses its row.
5. AWAITING_APPROVAL; respond posts the card and parks.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from langgraph.runtime import Runtime

from errand.agent.cards import book_card
from errand.agent.deps import RunDeps
from errand.agent.records import emit_swept, open_bookings
from errand.agent.state import SystemNote, ThreadState
from errand.agent.transitions import transition
from errand.agent.validation import Rejected, check_book_proposal, nearby_bookings
from errand.approvals import service
from errand.approvals.fingerprint import fingerprint
from errand.clock import from_ms, to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UiAction, UserMessage
from errand.domain.material import BookMaterial, ist_seconds
from errand.domain.outcomes import (
    CardReady,
    HoldFailed,
    OverBudget,
    Proposal,
    ProposalRefused,
    Replan,
    Requote,
    StepResult,
)
from errand.domain.slots import HoldRef, SlotHandle
from errand.domain.windows import slot_label
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.holds import HoldRow, hold_record_id_for
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import HoldSlotOut, ListBookingsOut, hold_is_live

_REFUSED_TEXT = {
    "no_shortlist": "That option list is out of date, so I didn't hold anything. "
    "Ask me to search again.",
    "stale_shortlist": "That option list is out of date, so I didn't hold anything. "
    "Ask me to search again.",
    "unknown_provider": "That provider isn't on the current list, so I didn't hold anything.",
    "unknown_slot": "That slot isn't on the current list, so I didn't hold anything.",
    "bad_slot_ref": "That slot isn't on the current list, so I didn't hold anything.",
    "slot_not_eligible": "That slot no longer fits your request, so I didn't hold it.",
    "duplicate_booking": "You already have that exact booking, so I didn't book it again.",
    "unresolved_intent": "That booking is still being confirmed with the provider; "
    "I'll tell you as soon as I know.",
    "unavailable": "I couldn't check your bookings just now, so I didn't hold anything. "
    "Nothing was booked.",
}


def ids_for(run_id: str, k: int) -> tuple[str, str]:
    """(proposal_id, approval_id) for the k-th proposal of a run (k = replans so far)."""
    suffix = run_id if k == 0 else f"{run_id}.{k}"
    return f"pr_{suffix}", f"ap_{suffix}"


async def prepare_proposal(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    assert state.errand is not None
    with deps.tracer.node("prepare_proposal", phase_before=state.errand.phase.value) as span:
        prep = _Preparer(deps, state)
        result = await prep.run()
        span.end_fields["phase_after"] = prep.errand.phase.value
    update: dict[str, Any] = {
        "errand": prep.errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }
    if isinstance(result, Replan):
        update["plan_result"] = None
        update["system_notes"] = [*state.system_notes, *prep.notes]
    return update


def route_after_prepare(state: ThreadState) -> str:
    return "plan" if isinstance(state.result, Replan) else "respond"


class _Preparer:
    def __init__(self, deps: RunDeps, state: ThreadState) -> None:
        assert state.errand is not None
        self.deps = deps
        self.state = state
        self.errand: Errand = state.errand
        self.notes: list[SystemNote] = []

    def _target(self) -> tuple[str, str] | ProposalRefused:
        """(provider_id, slot_ref) to prepare, from whatever asked for it."""
        state = self.state
        if isinstance(state.result, Requote):
            return self._handle_of_approval(state.result.approval_id)
        inbound = state.inbound
        if isinstance(inbound, UiAction) and inbound.kind == "select_slot":
            ref = inbound.payload.get("slot_ref", "")
            shortlist = self.errand.shortlist
            handle = shortlist.handle(ref) if shortlist is not None else None
            if handle is None:
                return ProposalRefused(reason="unknown_slot", text=_REFUSED_TEXT["unknown_slot"])
            return handle.provider_id, ref
        if isinstance(inbound, UiAction) and inbound.kind == "refresh_hold":
            approval_id = inbound.payload.get("approval_id") or self.errand.approval_id or ""
            return self._handle_of_approval(approval_id)
        plan = state.plan_result
        if isinstance(plan, Proposal) and plan.provider_id and plan.slot_ref:
            return plan.provider_id, plan.slot_ref
        return ProposalRefused(reason="unknown_slot", text=_REFUSED_TEXT["unknown_slot"])

    def _handle_of_approval(self, approval_id: str) -> tuple[str, str] | ProposalRefused:
        hold = self.errand.hold
        shortlist = self.errand.shortlist
        if hold is None or shortlist is None or self.errand.approval_id != approval_id:
            return ProposalRefused(reason="unknown_slot", text=_REFUSED_TEXT["unknown_slot"])
        for candidate in shortlist.candidates:
            for h in candidate.slots:
                if h.provider_id == hold.provider_id and h.slot.start == hold.slot.start:
                    return h.provider_id, h.ref
        return ProposalRefused(reason="stale_shortlist", text=_REFUSED_TEXT["stale_shortlist"])

    def _move(self, to: Phase, reason: str) -> None:
        self.errand = transition(self.errand, to, reason, self.deps.tracer)

    async def run(self) -> StepResult:
        result = await self._run()
        requote = self.state.result
        if isinstance(requote, Requote) and not isinstance(result, CardReady):
            # execute sent a granted card here; with no new card to replace it, it is
            # invalidated, never left granted (INV-16).
            deps = self.deps
            if await service.invalidate(deps.db, requote.approval_id, "hold_lapsed"):
                deps.tracer.emit(
                    "approval.invalidated", approval_id=requote.approval_id, reason="hold_lapsed"
                )
                deps.tracer.emit(
                    "card.update",
                    approval_id=requote.approval_id,
                    status="invalidated",
                    reason="hold_lapsed",
                )
            if self.errand.phase is Phase.AWAITING_APPROVAL:
                self._move(Phase.COMPARING, "approval_invalidated")
        return result

    async def _run(self) -> StepResult:
        deps = self.deps
        target = self._target()
        if isinstance(target, ProposalRefused):
            return target
        provider_id, slot_ref = target
        try:
            listed = await deps.tools.call(
                Caller.PROPOSER, "list_bookings", {"user_id": deps.user_id}
            )
        except ToolError:
            return ProposalRefused(reason="unavailable", text=_REFUSED_TEXT["unavailable"])
        if not isinstance(listed, ListBookingsOut):
            return ProposalRefused(reason="unavailable", text=_REFUSED_TEXT["unavailable"])
        verdict = check_book_proposal(
            self.errand,
            provider_id,
            slot_ref,
            now=deps.clock.now(),
            tz=deps.tz,
            bookings=listed.bookings,
            unresolved=await open_bookings(deps),
        )
        if isinstance(verdict, Rejected):
            deps.tracer.emit(
                "guard.planner_tool_rejected",
                detail={"tool": "prepare_proposal", "reason": verdict.code},
            )
            return ProposalRefused(
                reason=verdict.code, text=_REFUSED_TEXT.get(verdict.code, verdict.message)
            )
        was = self.errand.phase
        self._move(Phase.QUOTING, "proposal")
        handle = verdict.handle
        # The slot as the provider holds it. After execute's re-hold it is the window the
        # provider returned, which may differ from the shortlist's (S50).
        held_slot = handle.slot
        requote = self.state.result if isinstance(self.state.result, Requote) else None
        if requote is not None and requote.hold is not None:
            async with deps.db.session() as repos:
                hold = await repos.holds.get_by_hold_id(requote.hold.hold_id)
            held_slot = requote.hold.slot
        else:
            hold = await self._hold(handle)
        label = slot_label(held_slot, deps.tz)
        if hold is None:
            await self._not_approvable(was, "hold_failed")
            return self._replan_or(
                SystemNote(
                    kind="hold_failed",
                    text=f"The slot {slot_ref} with {provider_id} could not be held; "
                    "propose another slot or tell the user.",
                ),
                HoldFailed(provider_id=provider_id, slot_label=label),
            )
        budget = self.errand.brief.budget_max_inr
        if (
            budget is not None
            and isinstance(budget.value, int)
            and hold.quoted_price_inr > budget.value
        ):
            await self._not_approvable(was, "over_budget")
            return self._replan_or(
                SystemNote(
                    kind="over_budget",
                    text=(
                        f"The held quote for {provider_id} {slot_ref} is "
                        f"₹{hold.quoted_price_inr}, above the budget of ₹{budget.value}; it "
                        "cannot be approved. Propose a different provider within budget, or "
                        "tell the user."
                    ),
                ),
                OverBudget(
                    provider_id=provider_id,
                    provider_name=verdict.candidate.name,
                    slot_label=label,
                    quoted_price_inr=hold.quoted_price_inr,
                    budget_inr=budget.value,
                ),
            )
        nearby = nearby_bookings(listed.bookings, verdict.service, held_slot.start)
        material = BookMaterial(
            user_id=deps.user_id,
            provider_id=provider_id,
            service=verdict.service,
            slot_start=ist_seconds(held_slot.start),
            slot_end=ist_seconds(held_slot.end),
            quoted_price_inr=hold.quoted_price_inr,
            same_service_nearby=[b.booking_id for b in nearby],
            replaces_booking_id=self.errand.replaces_booking_id,
        )
        fp = fingerprint(material)
        proposal_id, approval_id = ids_for(self.state.run_id, self.state.counters.replans)
        now = deps.clock.now()
        expires = now + timedelta(milliseconds=deps.approval_policy.ttl_ms)
        card = book_card(
            approval_id=approval_id,
            errand_id=self.errand.errand_id,
            fingerprint=fp,
            material=material,
            candidate=verdict.candidate,
            slot=held_slot,
            search_price_inr=handle.search_price_inr,
            hold_id=hold.hold_id,
            hold_expires_at=from_ms(hold.expires_at),
            expires_at=expires,
            nearby=nearby,
            tz=deps.tz,
            brief=self.errand.brief,
            rebook=self.errand.rebook if self.errand.replaces_booking_id else None,
        )
        row = ApprovalRow(
            approval_id=approval_id,
            conversation_id=deps.conversation_id,
            errand_id=self.errand.errand_id,
            proposal_id=proposal_id,
            action="book",
            fingerprint=fp,
            material_json=material.model_dump_json(),
            card_json=card.model_dump_json(),
            bound_hold_id=hold.hold_id,
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
            deps.tracer.emit(
                "approval.invalidated", approval_id=old, reason="superseded", new_fp=fp
            )
            deps.tracer.emit(
                "card.update", approval_id=old, status="invalidated", reason="superseded"
            )
        if created.created:
            deps.tracer.emit(
                "approval.requested",
                approval_id=approval_id,
                fingerprint=fp,
                hold_id=hold.hold_id,
                expires_at=from_ms(row.expires_at).isoformat(),
                superseded=created.superseded,
            )
        self._move(Phase.AWAITING_APPROVAL, "card_shown")
        self.errand = self.errand.model_copy(
            update={
                "approval_id": approval_id,
                "hold": HoldRef(
                    hold_record_id=hold.hold_record_id,
                    hold_id=hold.hold_id,
                    provider_id=provider_id,
                    slot=held_slot,
                    search_price_inr=handle.search_price_inr,
                    quoted_price_inr=hold.quoted_price_inr,
                    expires_at=from_ms(hold.expires_at),
                ),
            }
        )
        replaced = requote.reason if requote is not None else None
        detail = requote.detail if requote is not None else None
        return CardReady(approval_id=approval_id, replaced_reason=replaced, replaced_detail=detail)

    async def _not_approvable(self, was: Phase, reason: str) -> None:
        """No new card: back to where the errand was, without leaving a card behind.

        A card that was already on screen stays live and the errand goes back to waiting
        for it. A granted card sent here by execute (a lapsed hold) cannot be booked on
        and has no replacement, so it is invalidated rather than left granted.
        """
        deps = self.deps
        if isinstance(self.state.result, Requote):
            self._move(Phase.COMPARING, reason)
            return
        async with deps.db.session() as repos:
            live = await repos.approvals.live_for_conversation(deps.conversation_id)
        if was is Phase.AWAITING_APPROVAL and live is not None and live.status == "pending":
            self._move(Phase.AWAITING_APPROVAL, f"{reason}_card_kept")
        else:
            self._move(Phase.COMPARING, reason)

    def _replan_or(self, note: SystemNote, otherwise: StepResult) -> StepResult:
        """Back to plan once per run, and only when a user message started it."""
        deps = self.deps
        if (
            isinstance(self.state.inbound, UserMessage)
            and deps.counters.replans < deps.limits.replans_per_run
        ):
            deps.counters.replans += 1
            self.notes.append(note)
            return Replan(note=note.kind)  # type: ignore[arg-type]
        return otherwise

    async def _hold(self, handle: SlotHandle) -> HoldRow | None:
        deps = self.deps
        start_ms = to_ms(handle.slot.start)
        async with deps.db.session() as repos:
            latest = await repos.holds.latest(self.errand.errand_id, handle.provider_id, start_ms)
        margin = timedelta(seconds=deps.timeouts.book + deps.approval_settings.hold_margin_s)
        now = deps.clock.now()
        # A hold is reused only while it is live by the world's clock with room to book.
        if latest is not None and hold_is_live(now + margin, from_ms(latest.expires_at)):
            return latest
        generation = latest.generation + 1 if latest is not None else 0
        try:
            answer = await deps.tools.call(
                Caller.PROPOSER,
                "hold_slot",
                {"provider_id": handle.provider_id, "slot": handle.slot},
            )
        except ToolError:
            return None
        if not isinstance(answer, HoldSlotOut):
            return None
        held_start = to_ms(answer.slot.start)
        if held_start != start_ms or answer.slot.end != handle.slot.end:
            # The provider moved the slot (a test-only fault, M5's slot_changed path).
            return None
        async with deps.db.session() as repos:
            return await repos.holds.insert(
                HoldRow(
                    hold_record_id=hold_record_id_for(
                        self.errand.errand_id, handle.provider_id, start_ms, generation
                    ),
                    errand_id=self.errand.errand_id,
                    provider_id=handle.provider_id,
                    slot_start=start_ms,
                    generation=generation,
                    hold_id=answer.hold_id,
                    quoted_price_inr=answer.quoted_price_inr,
                    expires_at=to_ms(answer.expires_at),
                )
            )
