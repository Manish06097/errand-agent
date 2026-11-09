"""execute for a cancel or a reschedule (architecture 9.4 steps 1 to 5, 9.5, N1).

Dispatched from the `execute` node on the approval's action; the booking classes stay as
they are. Code only, under the per-user consequential lock (held through `verify`):

1. Re-entry first: an intent for this approval means it was already acted on. A terminal
   intent returns its outcome; an unresolved one goes to `verify`. No new key is made.
2. Preflight, no side effects: the row is `granted` and within its grace, and the
   material recomputed from a fresh `list_bookings` (the booking, its provider, slot and
   status; for a reschedule also the approved new slot) has the approved fingerprint. A
   change invalidates the approval (INV-12) and ends with `booking_changed`.
3. The guard transaction (approvals.service.consume): re-check that no other cancel or
   reschedule works on the booking, consume (exactly one row), write the intent and
   attempt 1 as `prepared` with the key from `derive_idem_key`. Fault point
   `after_intent_write`.
4. The attempt moves to `in_flight` (committed before the call, so `prepared` is provably
   unsent), then the call goes out under `asyncio.shield`. A cancel's 503 is a
   `ToolTransient`: retried inside the same attempt with the same key, at most 3 calls
   with 1 s and 2 s of simulated backoff, each a `tool.call` with `attempt=n` and each
   counted on the attempt before it is sent. Fault point `after_world_book`.
5. Whatever comes back, a clean `cancelled` included, goes to `verify`, which reads
   `list_bookings`: success is not trusted. Only a definitive refusal (`invalid_args`,
   `slot_conflict`) ends here: the attempt `not_applied`, the intent `failed`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from errand.agent.deps import RunDeps
from errand.agent.records import emit_swept
from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.approvals import service
from errand.approvals.fingerprint import fingerprint, parse_material
from errand.clock import to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import ApprovalGranted
from errand.domain.material import CancelMaterial, RescheduleMaterial, ist_seconds
from errand.domain.outcomes import (
    AlreadyHandled,
    BookCalled,
    Changed,
    ChangeFailed,
    PendingCheck,
    StepResult,
)
from errand.execution import changes as drafts
from errand.execution.executor import ConsequentialClient
from errand.faults import fault_point
from errand.store.agent_db import Repos
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.intents import AttemptRow, IntentRow
from errand.tools.errors import ToolError, ToolTransient
from errand.tools.gateway import Caller
from errand.world.contracts import (
    BookingItem,
    CancelError,
    CancelOut,
    ListBookingsOut,
    RescheduleError,
    RescheduleOut,
    Slot,
)

CANCEL_CALLS = 3
CANCEL_BACKOFF_S = (1.0, 2.0)
TERMINAL = frozenset({"succeeded", "failed"})
Material = CancelMaterial | RescheduleMaterial


async def action_of(deps: RunDeps, approval_id: str) -> str:
    """`book`, `cancel` or `reschedule`: what the approval being executed is for."""
    async with deps.db.session() as repos:
        row = await repos.approvals.get(approval_id)
    return row.action if row is not None else "book"


async def run_node(deps: RunDeps, state: ThreadState) -> dict[str, Any]:
    assert state.errand is not None and isinstance(state.inbound, ApprovalGranted)
    with deps.tracer.node("execute", phase_before=state.errand.phase.value) as span:
        ex = _ExecuteChange(deps, state.errand, state.inbound.approval_id)
        try:
            result = await ex.run()
        finally:
            if not isinstance(ex.result, BookCalled):
                # Only an attempt handed to verify keeps the lock; verify releases it.
                await deps.release_held()
        span.end_fields["phase_after"] = ex.errand.phase.value
    return {"errand": ex.errand, "result": result, "counters": deps.counters.model_copy(deep=True)}


def current_material(approved: Material, bookings: list[BookingItem]) -> Material | None:
    """The approved material recomputed from a fresh list: None when the booking is gone
    or no longer confirmed (nothing to fingerprint)."""
    booking = next((b for b in bookings if b.booking_id == approved.booking_id), None)
    if booking is None or booking.status != "confirmed":
        return None
    if isinstance(approved, CancelMaterial):
        return CancelMaterial(
            user_id=approved.user_id,
            booking_id=booking.booking_id,
            provider_id=booking.provider_id,
            slot_start=ist_seconds(booking.slot.start),
        )
    return RescheduleMaterial(
        user_id=approved.user_id,
        booking_id=booking.booking_id,
        provider_id=booking.provider_id,
        from_start=ist_seconds(booking.slot.start),
        to_start=approved.to_start,
        to_end=approved.to_end,
    )


def subject_of(material: Material) -> str:
    """The key subject (9.3): the booking, plus the new slot for a reschedule."""
    if isinstance(material, CancelMaterial):
        return material.booking_id
    return f"{material.booking_id}|{datetime.fromisoformat(material.to_start).isoformat()}"


class _ExecuteChange:
    def __init__(self, deps: RunDeps, errand: Errand, approval_id: str) -> None:
        self.deps = deps
        self.errand = errand
        self.approval_id = approval_id
        self.result: StepResult | None = None

    def _move(self, to: Phase, reason: str) -> None:
        self.errand = transition(self.errand, to, reason, self.deps.tracer)

    async def run(self) -> StepResult:
        self.result = await self._run()
        return self.result

    async def _run(self) -> StepResult:
        deps = self.deps
        assert deps.consequential is not None, "the RunManager supplies the user locks"
        held = await deps.consequential.acquire(deps.user_id)
        deps.held.append(held)

        async with deps.db.session() as repos:
            intent = await repos.intents.get_by_approval(self.approval_id)
        if intent is not None:
            return await self._reentry(intent)

        now = deps.clock.now()
        _, swept = await service.live_approval(
            deps.db, deps.conversation_id, to_ms(now), deps.approval_policy
        )
        emit_swept(deps.tracer, swept)
        async with deps.db.session() as repos:
            row = await repos.approvals.get(self.approval_id)
        if row is None or row.status != "granted":
            status = row.status if row is not None else "missing"
            return AlreadyHandled(approval_id=self.approval_id, reason=status)
        approved = parse_material(row.material_json)
        assert isinstance(approved, CancelMaterial | RescheduleMaterial)
        action = approved.action

        try:
            listed = await deps.tools.call(
                Caller.EXECUTOR, "list_bookings", {"user_id": deps.user_id}, critical=True
            )
        except ToolError:
            return AlreadyHandled(approval_id=self.approval_id, reason="bookings_unavailable")
        if not isinstance(listed, ListBookingsOut):
            return AlreadyHandled(approval_id=self.approval_id, reason="bookings_unavailable")
        current = current_material(approved, list(listed.bookings))
        current_fp = fingerprint(current) if current is not None else None
        if current_fp != row.fingerprint:
            return await self._changed(row, current_fp, action)

        intent_row = drafts.change_intent(
            action=action,
            approval_id=self.approval_id,
            conversation_id=deps.conversation_id,
            errand_id=self.errand.errand_id,
            user_id=deps.user_id,
            fingerprint=row.fingerprint,
            material_json=row.material_json,
            booking_id=approved.booking_id,
            now_ms=to_ms(now),
        )
        attempt = drafts.first_change_attempt(
            intent_row, subject=subject_of(approved), fence_epoch=held.epoch, now_ms=to_ms(now)
        )

        async def recheck(repos: Repos) -> str | None:
            # Inside the guard transaction, after the user lock serialised us: another
            # conversation's change to this booking is visible here.
            for other in await repos.intents.unresolved_for_user(deps.user_id):
                if other.approval_id == self.approval_id:
                    continue
                if other.target_booking_id == approved.booking_id and other.action != "book":
                    return "duplicate_risk"
            return None

        async def write(repos: Repos) -> None:
            await repos.intents.insert_intent(intent_row)
            await repos.intents.insert_attempt(attempt)

        token = await service.consume(
            deps.db,
            approval_id=self.approval_id,
            fingerprint=row.fingerprint,
            intent_id=intent_row.intent_id,
            now_ms=to_ms(now),
            policy=deps.approval_policy,
            recheck=recheck,
            write=write,
        )
        if isinstance(token, service.ConsumeRefused):
            if token.invalidated:
                self._emit_invalidated(row, token.reason, None)
                self._move(Phase.COMPARING, "approval_invalidated")
                return ChangeFailed(
                    action=action, approval_id=self.approval_id, reason="in_progress"
                )
            deps.tracer.emit(
                "approval.consume_refused",
                approval_id=self.approval_id,
                reason=token.reason,
                status=token.status,
            )
            return AlreadyHandled(approval_id=self.approval_id, reason=token.reason)

        deps.intents.add(intent_row.intent_id)
        tracer = deps.tracer
        tracer.emit(
            "approval.consumed",
            approval_id=self.approval_id,
            fingerprint=row.fingerprint,
            intent_id=intent_row.intent_id,
        )
        tracer.emit("card.update", approval_id=self.approval_id, status="consumed")
        tracer.emit(
            "intent.written",
            intent_id=intent_row.intent_id,
            attempt=1,
            idempotency_key=attempt.idempotency_key,
            state="prepared",
        )
        self._move(Phase.EXECUTING, "approval_granted")
        fault_point("after_intent_write")

        async with deps.db.transaction() as repos:
            moved = await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("prepared",),
                to="in_flight",
                fence_epoch=held.epoch,
                now_ms=to_ms(deps.clock.now()),
                add_calls=1,
            )
        if not moved:
            tracer.emit(
                "approval.consume_refused",
                approval_id=self.approval_id,
                reason="attempt_fenced_off",
                status="consumed",
            )
            return PendingCheck(
                approval_id=self.approval_id, intent_id=intent_row.intent_id, reason="fenced_off"
            )
        tracer.emit(
            "intent.in_flight",
            intent_id=intent_row.intent_id,
            attempt=1,
            idempotency_key=attempt.idempotency_key,
            state="in_flight",
        )
        return await self._send(token, intent_row, attempt, approved, held.epoch)

    async def _send(
        self,
        token: service.ApprovalToken,
        intent: IntentRow,
        attempt: AttemptRow,
        approved: Material,
        epoch: int,
    ) -> StepResult:
        """The call(s) of attempt 1, then the classification."""
        deps = self.deps
        client = ConsequentialClient(deps.tools)
        answer: CancelOut | CancelError | RescheduleOut | RescheduleError | None = None
        error: str | None = None
        for n in range(1, CANCEL_CALLS + 1):
            if n > 1:
                # Write-ahead for every re-send: the count is committed before the call.
                async with deps.db.transaction() as repos:
                    await repos.intents.move_attempt(
                        attempt.attempt_id,
                        frm=("in_flight",),
                        to="in_flight",
                        fence_epoch=epoch,
                        now_ms=to_ms(deps.clock.now()),
                        add_calls=1,
                    )
            try:
                if isinstance(approved, CancelMaterial):
                    answer = await client.cancel(
                        token,
                        intent_id=intent.intent_id,
                        booking_id=approved.booking_id,
                        idempotency_key=attempt.idempotency_key,
                        attempt=n,
                    )
                else:
                    answer = await client.reschedule(
                        token,
                        intent_id=intent.intent_id,
                        booking_id=approved.booking_id,
                        slot=_new_slot(approved),
                        idempotency_key=attempt.idempotency_key,
                        attempt=n,
                    )
                error = None
                break
            except ToolTransient as exc:
                # A 503: retried with the same key inside this attempt (9.5).
                error = type(exc).__name__
                if n == CANCEL_CALLS or isinstance(approved, RescheduleMaterial):
                    break
                await deps.clock.sleep(CANCEL_BACKOFF_S[n - 1])
            except ToolError as exc:
                error = type(exc).__name__
                break
        fault_point("after_world_book")
        return await self._classify(intent, attempt, epoch, approved, answer, error)

    async def _classify(
        self,
        intent: IntentRow,
        attempt: AttemptRow,
        epoch: int,
        approved: Material,
        answer: CancelOut | CancelError | RescheduleOut | RescheduleError | None,
        error: str | None,
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        if isinstance(answer, CancelError | RescheduleError):
            # The provider answered for this request: definitively not applied.
            async with deps.db.transaction() as repos:
                await repos.intents.move_attempt(
                    attempt.attempt_id,
                    frm=("in_flight",),
                    to="not_applied",
                    fence_epoch=epoch,
                    now_ms=now_ms,
                    last_error=answer.error,
                )
                await repos.intents.resolve_intent(
                    intent.intent_id,
                    frm=("open",),
                    to="failed",
                    result_json=json.dumps({"error": answer.error}),
                    now_ms=now_ms,
                )
            deps.tracer.emit(
                "intent.resolved", intent_id=intent.intent_id, state="failed", error=answer.error
            )
            self._move(Phase.COMPARING, answer.error)
            return ChangeFailed(
                action=approved.action,
                approval_id=self.approval_id,
                reason="slot_conflict" if answer.error == "slot_conflict" else "rejected",
            )
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("in_flight",),
                to="unknown",
                fence_epoch=epoch,
                now_ms=now_ms,
                last_error=error,
            )
        deps.tracer.emit(
            "intent.unknown", intent_id=intent.intent_id, attempt=1, state="unknown", error=error
        )
        return BookCalled(
            approval_id=self.approval_id,
            intent_id=intent.intent_id,
            attempt_id=attempt.attempt_id,
            answer="confirmed" if answer is not None else "unknown",
            booking_id=approved.booking_id,
        )

    async def _reentry(self, intent: IntentRow) -> StepResult:
        deps = self.deps
        if intent.state == "succeeded":
            return Changed(
                action="cancel" if intent.action == "cancel" else "reschedule",
                approval_id=self.approval_id,
                intent_id=intent.intent_id,
                booking_id=intent.target_booking_id or "",
                by_provider=bool(json.loads(intent.result_json or "{}").get("by_provider")),
            )
        if intent.state in TERMINAL:
            return AlreadyHandled(approval_id=self.approval_id, reason="not_consumable")
        async with deps.db.session() as repos:
            attempts = await repos.intents.attempts(intent.intent_id)
        if self.errand.phase is Phase.AWAITING_APPROVAL:
            self._move(Phase.EXECUTING, "approval_granted")
        return BookCalled(
            approval_id=self.approval_id,
            intent_id=intent.intent_id,
            attempt_id=attempts[-1].attempt_id,
            answer="unknown",
            booking_id=intent.target_booking_id,
        )

    async def _changed(self, row: ApprovalRow, new_fp: str | None, action: str) -> StepResult:
        await service.invalidate(self.deps.db, row.approval_id, "material_mismatch")
        self._emit_invalidated(row, "material_mismatch", new_fp)
        self._move(Phase.COMPARING, "approval_invalidated")
        return ChangeFailed(
            action="cancel" if action == "cancel" else "reschedule",
            approval_id=row.approval_id,
            reason="booking_changed",
        )

    def _emit_invalidated(self, row: ApprovalRow, reason: str, new_fp: str | None) -> None:
        tracer = self.deps.tracer
        tracer.emit(
            "approval.invalidated",
            approval_id=row.approval_id,
            reason=reason,
            old_fp=row.fingerprint,
            new_fp=new_fp,
        )
        tracer.emit("card.update", approval_id=row.approval_id, status="invalidated", reason=reason)


def _new_slot(material: RescheduleMaterial) -> Slot:
    return Slot(
        start=datetime.fromisoformat(material.to_start),
        end=datetime.fromisoformat(material.to_end),
    )
