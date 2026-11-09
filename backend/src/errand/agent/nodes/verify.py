"""verify: the source of truth decides what `book` did (architecture 9.4 step 6, G7).

`get_booking_status(idempotency_key)` is called before anything is reported, even
after a clean `confirmed`. A confirmed status is then compared with the consumed
approval's material: provider and slot always (the status's slot, and `list_bookings`
for the provider), and the price whenever the `book` answer carried it (after a
timeout it did not, and the record says `price_unverified`).

- confirmed and matching: attempt `applied`, intent `succeeded`, `known_bookings`
  upserted, COMPLETED, BOOKED_VERIFIED;
- confirmed but different: attempt `applied`, intent `needs_attention`,
  NEEDS_ATTENTION, and the user is told plainly (M5 adds the cancel card);
- anything else after an unknown answer (not_found, status down): intent
  `needs_attention`, PENDING_VERIFICATION. M4 never re-sends `book`: no same-key
  retry and no second attempt (M5).

The consequential lock taken in `execute` is released here, whatever happens.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from langgraph.runtime import Runtime

from errand.agent.changes import verify as changes_verify
from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.approvals.fingerprint import parse_material
from errand.clock import from_ms, to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.material import BookMaterial
from errand.domain.outcomes import BookCalled, Booked, PendingCheck, StepResult
from errand.store.repos.intents import AttemptRow
from errand.store.repos.known_bookings import KnownBooking
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookingStatusOut, ListBookingsOut, hold_is_live


async def verify(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    assert state.errand is not None and isinstance(state.result, BookCalled)
    if await changes_verify.is_change(deps, state.result.intent_id):
        return await changes_verify.run_node(deps, state)  # cancel, reschedule (N1)
    with deps.tracer.node("verify", phase_before=state.errand.phase.value) as span:
        check = _Verify(deps, state.errand, state.result)
        try:
            result = await check.run()
        finally:
            await deps.release_held()
        span.end_fields["phase_after"] = check.errand.phase.value
    return {
        "errand": check.errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }


class _Verify:
    def __init__(self, deps: RunDeps, errand: Errand, called: BookCalled) -> None:
        self.deps = deps
        self.errand = errand
        self.called = called

    def _move(self, to: Phase, reason: str) -> None:
        self.errand = transition(self.errand, to, reason, self.deps.tracer)

    async def run(self) -> StepResult:
        deps = self.deps
        called = self.called
        async with deps.db.session() as repos:
            attempt = await repos.intents.get_attempt(called.attempt_id)
            intent = await repos.intents.get(called.intent_id)
        assert attempt is not None and intent is not None
        material = parse_material(intent.material_json)
        assert isinstance(material, BookMaterial)
        key = attempt.idempotency_key
        self._move(
            Phase.VERIFYING, "book_confirmed" if called.answer == "confirmed" else "book_unknown"
        )
        status = await self._status(key)
        hold_live = (
            hold_is_live(deps.clock.now(), from_ms(attempt.hold_expires_at))
            if attempt.hold_expires_at is not None
            else None
        )
        if status is None or status.status != "confirmed" or status.booking_id is None:
            deps.tracer.emit(
                "status.check",
                idempotency_key=key,
                result=status.model_dump(mode="json") if status else None,
                hold_live=hold_live,
                decision="escalate",
            )
            return await self._unresolved(attempt, "status_not_confirmed", "")
        mismatch = await self._compare(material, status)
        price_unverified = called.price_inr is None
        deps.tracer.emit(
            "status.check",
            idempotency_key=key,
            result=status.model_dump(mode="json"),
            hold_live=hold_live,
            decision="escalate" if mismatch else "report_success",
            price_unverified=price_unverified or None,
            mismatch=mismatch or None,
        )
        if mismatch:
            return await self._unresolved(
                attempt, "material_mismatch", "; ".join(mismatch), applied=True
            )
        booking_id = status.booking_id
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight"),
                to="applied",
                fence_epoch=attempt.fence_epoch,
                now_ms=now_ms,
                add_status_checks=1,
            )
            await repos.intents.resolve_intent(
                intent.intent_id,
                frm=("open", "needs_attention"),
                to="succeeded",
                result_json=json.dumps({"booking_id": booking_id}),
                now_ms=now_ms,
            )
            await repos.known_bookings.upsert(
                KnownBooking(
                    booking_id=booking_id,
                    user_id=deps.user_id,
                    provider_id=material.provider_id,
                    service=material.service.value,
                    slot_start=to_ms(datetime.fromisoformat(material.slot_start)),
                    slot_end=to_ms(datetime.fromisoformat(material.slot_end)),
                    status="confirmed",
                    last_seen_at=now_ms,
                    origin_conversation_id=deps.conversation_id,
                    origin_brief_json=self.errand.brief.model_dump_json(),
                    source_intent_id=intent.intent_id,
                )
            )
        deps.tracer.emit(
            "intent.resolved",
            intent_id=intent.intent_id,
            attempt=attempt.n,
            idempotency_key=key,
            state="succeeded",
            booking_id=booking_id,
            book_calls=attempt.calls,
        )
        self._move(Phase.COMPLETED, "status_confirmed")
        return Booked(
            approval_id=called.approval_id, intent_id=intent.intent_id, booking_id=booking_id
        )

    async def _status(self, key: str) -> BookingStatusOut | None:
        try:
            answer = await self.deps.tools.call(
                Caller.VERIFIER, "get_booking_status", {"idempotency_key": key}, critical=True
            )
        except ToolError:
            return None
        return answer if isinstance(answer, BookingStatusOut) else None

    async def _compare(self, material: BookMaterial, status: BookingStatusOut) -> list[str]:
        """What the provider recorded that differs from what the user approved."""
        called = self.called
        problems: list[str] = []
        if called.booking_id is not None and status.booking_id != called.booking_id:
            problems.append(f"booking {status.booking_id}, but book answered {called.booking_id}")
        start = datetime.fromisoformat(material.slot_start)
        end = datetime.fromisoformat(material.slot_end)
        if status.slot is None or status.slot.start != start or status.slot.end != end:
            problems.append("a different slot")
        if called.price_inr is not None and called.price_inr != material.quoted_price_inr:
            problems.append(f"₹{called.price_inr} instead of ₹{material.quoted_price_inr}")
        try:
            listed = await self.deps.tools.call(
                Caller.VERIFIER, "list_bookings", {"user_id": self.deps.user_id}, critical=True
            )
        except ToolError:
            listed = None
        if not isinstance(listed, ListBookingsOut):
            problems.append("the provider could not be checked")
        else:
            mine = next((b for b in listed.bookings if b.booking_id == status.booking_id), None)
            if mine is None or mine.provider_id != material.provider_id:
                problems.append("a different provider")
        return problems

    async def _unresolved(
        self,
        attempt: AttemptRow,
        reason: Literal["status_not_confirmed", "material_mismatch"],
        detail: str,
        *,
        applied: bool = False,
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight"),
                to="applied" if applied else "unknown",
                fence_epoch=attempt.fence_epoch,
                now_ms=now_ms,
                add_status_checks=1,
            )
            await repos.intents.resolve_intent(
                attempt.intent_id,
                frm=("open",),
                to="needs_attention",
                result_json=json.dumps({"reason": reason, "detail": detail}),
                now_ms=now_ms,
            )
        deps.tracer.emit(
            "intent.resolved",
            intent_id=attempt.intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            state="needs_attention",
            book_calls=attempt.calls,
        )
        self._move(Phase.NEEDS_ATTENTION, reason)
        return PendingCheck(
            approval_id=self.called.approval_id,
            intent_id=attempt.intent_id,
            reason=reason,
            detail=detail,
        )
