"""verify for a cancel or a reschedule (architecture 9.5, N1).

Verification reads `list_bookings` only (its contract returns status and slot). It is
called before anything is reported, even after a clean `cancelled` answer:

- cancel: the booking shows `cancelled` -> attempt `applied`, intent `succeeded`,
  CANCELLED_VERIFIED. It shows `cancelled_by_provider` -> the same end state, reported
  as the provider's doing ("already cancelled by the provider"), never as ours;
- reschedule: the booking shows the approved new slot -> RESCHEDULED_VERIFIED; it shows
  `cancelled_by_provider` -> a failure with that reason;
- the booking is still `confirmed` (the old slot, for a reschedule) after the provider
  never answered (a timeout, or every 503): the attempt is `not_applied`, the intent
  `failed`, ACTION_FAILED "nothing was cancelled". A 503 is not proof of "not applied",
  and that is why this reads the list instead of trusting the error. A provider that
  said `cancelled` while the list still shows `confirmed` is a contradiction: the intent
  is `needs_attention`, never claimed;
- `list_bookings` unavailable, or the booking missing from it: `needs_attention` with
  PENDING_VERIFICATION, re-checked by the reconciler.

The consequential lock taken in `execute` is released here, whatever happens.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.approvals.fingerprint import parse_material
from errand.clock import to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.material import CancelMaterial, RescheduleMaterial
from errand.domain.outcomes import (
    BookCalled,
    Changed,
    ChangeFailed,
    PendingCheck,
    StepResult,
)
from errand.store.repos.intents import AttemptRow, IntentRow
from errand.store.repos.known_bookings import KnownBooking
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookingItem, ListBookingsOut


async def is_change(deps: RunDeps, intent_id: str) -> bool:
    """Is this intent a cancel or a reschedule (so the node dispatches here)?"""
    async with deps.db.session() as repos:
        intent = await repos.intents.get(intent_id)
    return intent is not None and intent.action != "book"


async def run_node(deps: RunDeps, state: ThreadState) -> dict[str, Any]:
    assert state.errand is not None and isinstance(state.result, BookCalled)
    with deps.tracer.node("verify", phase_before=state.errand.phase.value) as span:
        check = _VerifyChange(deps, state.errand, state.result)
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


class _VerifyChange:
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
        assert isinstance(material, CancelMaterial | RescheduleMaterial)
        action = material.action
        self._move(
            Phase.VERIFYING, "change_called" if called.answer == "confirmed" else "change_unknown"
        )
        listed = await self._list()
        if listed is None:
            return await self._unresolved(attempt, intent, "status_not_confirmed", "")
        booking = next((b for b in listed.bookings if b.booking_id == material.booking_id), None)
        if booking is None:
            deps.tracer.emit(
                "status.check",
                idempotency_key=attempt.idempotency_key,
                result={"booking_id": material.booking_id, "listed": False},
                decision="escalate",
            )
            return await self._unresolved(attempt, intent, "status_not_confirmed", "")
        outcome = _judge(material, booking)
        deps.tracer.emit(
            "status.check",
            idempotency_key=attempt.idempotency_key,
            result={
                "booking_id": booking.booking_id,
                "status": booking.status,
                "slot_start": booking.slot.start.isoformat(),
            },
            decision={
                "applied": "report_success",
                "by_provider": "report_success",
                "not_applied": "report_not_applied",
                "provider_cancelled": "report_not_applied",
            }.get(outcome, "escalate"),
        )
        if outcome in ("applied", "by_provider"):
            return await self._succeeded(
                attempt, intent, booking, action, by_provider=outcome == "by_provider"
            )
        if outcome == "provider_cancelled":
            return await self._failed(attempt, intent, "provider_cancelled", action)
        if outcome == "not_applied":
            if called.answer == "confirmed":
                # The provider said it was done and its own list disagrees.
                return await self._unresolved(
                    attempt,
                    intent,
                    "material_mismatch",
                    "the provider said it was done, but still lists the old booking",
                )
            return await self._failed(attempt, intent, "still_confirmed", action)
        return await self._failed(attempt, intent, "booking_changed", action)

    async def _list(self) -> ListBookingsOut | None:
        try:
            answer = await self.deps.tools.call(
                Caller.VERIFIER, "list_bookings", {"user_id": self.deps.user_id}, critical=True
            )
        except ToolError:
            return None
        return answer if isinstance(answer, ListBookingsOut) else None

    async def _succeeded(
        self,
        attempt: AttemptRow,
        intent: IntentRow,
        booking: BookingItem,
        action: str,
        *,
        by_provider: bool,
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        result = {
            "booking_id": booking.booking_id,
            "provider_id": booking.provider_id,
            "provider_name": booking.provider_name,
            "service": booking.service.value,
            "slot_start": booking.slot.start.isoformat(),
            "by_provider": by_provider,
        }
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
                result_json=json.dumps(result),
                now_ms=now_ms,
            )
            await repos.known_bookings.upsert(
                KnownBooking(
                    booking_id=booking.booking_id,
                    user_id=deps.user_id,
                    provider_id=booking.provider_id,
                    service=booking.service.value,
                    slot_start=to_ms(booking.slot.start),
                    slot_end=to_ms(booking.slot.end),
                    status=booking.status,
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
            idempotency_key=attempt.idempotency_key,
            state="succeeded",
            booking_id=booking.booking_id,
            book_calls=attempt.calls,
        )
        self._move(Phase.COMPLETED, "list_bookings_confirmed")
        return Changed(
            action="cancel" if action == "cancel" else "reschedule",
            approval_id=self.called.approval_id,
            intent_id=intent.intent_id,
            booking_id=booking.booking_id,
            by_provider=by_provider,
        )

    async def _failed(
        self, attempt: AttemptRow, intent: IntentRow, reason: str, action: str
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight", "prepared"),
                to="not_applied",
                fence_epoch=attempt.fence_epoch,
                now_ms=now_ms,
                add_status_checks=1,
                last_error=reason,
            )
            await repos.intents.resolve_intent(
                intent.intent_id,
                frm=("open", "needs_attention"),
                to="failed",
                result_json=json.dumps({"error": reason}),
                now_ms=now_ms,
            )
        deps.tracer.emit(
            "intent.resolved",
            intent_id=intent.intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            state="failed",
            error=reason,
            book_calls=attempt.calls,
        )
        self._move(Phase.FAILED, reason)
        return ChangeFailed(
            action="cancel" if action == "cancel" else "reschedule",
            approval_id=self.called.approval_id,
            reason=reason,  # type: ignore[arg-type]
        )

    async def _unresolved(
        self, attempt: AttemptRow, intent: IntentRow, reason: str, detail: str
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight"),
                to="unknown",
                fence_epoch=attempt.fence_epoch,
                now_ms=now_ms,
                add_status_checks=1,
            )
            await repos.intents.resolve_intent(
                intent.intent_id,
                frm=("open", "needs_attention"),
                to="needs_attention",
                result_json=json.dumps({"reason": reason, "detail": detail}),
                now_ms=now_ms,
            )
        deps.tracer.emit(
            "intent.resolved",
            intent_id=intent.intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            state="needs_attention",
            book_calls=attempt.calls,
        )
        self._move(Phase.NEEDS_ATTENTION, reason)
        return PendingCheck(
            approval_id=self.called.approval_id,
            intent_id=intent.intent_id,
            reason=reason,  # type: ignore[arg-type]
            detail=detail,
        )


def _judge(material: CancelMaterial | RescheduleMaterial, booking: BookingItem) -> str:
    """What `list_bookings` says about the approved change: `applied`, `by_provider`,
    `provider_cancelled`, `not_applied` (the old state) or `changed` (anything else)."""
    if isinstance(material, CancelMaterial):
        if booking.status == "cancelled":
            return "applied"
        if booking.status == "cancelled_by_provider":
            return "by_provider"
        if booking.status == "confirmed" and booking.slot.start == datetime.fromisoformat(
            material.slot_start
        ):
            return "not_applied"
        return "changed"
    if booking.status == "cancelled_by_provider":
        return "provider_cancelled"
    if booking.status != "confirmed":
        return "changed"
    if booking.slot.start == datetime.fromisoformat(material.to_start):
        return "applied"
    if booking.slot.start == datetime.fromisoformat(material.from_start):
        return "not_applied"
    return "changed"
