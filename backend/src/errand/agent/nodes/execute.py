"""execute: from a granted approval to one `book` call (architecture 8.4, 9.4). Code only.

Under the per-user consequential lock (a `user:<id>` lease plus an in-process lock,
held through `verify`):

1. Re-entry first: an intent for this approval means this approval was already acted
   on. A terminal intent returns its outcome; an unresolved one goes to `verify`. No
   new key is ever made for it.
2. Preflight, no side effects: the row is `granted` and within its grace; the bound
   hold is still live with room to book; the material recomputed from current facts
   (the held quote, a fresh `list_bookings` for the nearby bookings) has the approved
   fingerprint, and no exact duplicate exists. A change invalidates the approval and
   sends the errand back to `prepare_proposal` for a new card (INV-12); a lapsed or
   nearly lapsed hold goes the same way (M4's stand-in for M5's re-hold).
3. The guard transaction (approvals.service.consume): the duplicate re-check against
   current rows, the consume (exactly one row), the intent and attempt 1 as `prepared`
   with the hold's expiry, the idempotency key from `derive_idem_key` and `fence_epoch`
   from the user lease. Commit; only then is the ApprovalToken minted.
4. The attempt moves to `in_flight` (committed, so a `prepared` attempt is provably
   unsent), and `book` goes out under `asyncio.shield` with the key read back from the
   row. Any answer except a definitive refusal goes to `verify`: a clean `confirmed` is
   not trusted until `get_booking_status` agrees.

Definitive answers end here, with nothing re-sent in M4: `slot_taken` and `hold_expired`
make the attempt `not_applied`, the intent `failed` and the errand COMPARING, and the
user hears that nothing was booked (`hold_expired` also offers a fresh quote).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal

from langgraph.runtime import Runtime

from errand.agent.deps import RunDeps
from errand.agent.records import emit_swept
from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.agent.validation import duplicate_of, nearby_bookings
from errand.approvals import service
from errand.approvals.fingerprint import fingerprint, parse_material
from errand.clock import to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import ApprovalGranted
from errand.domain.material import BookMaterial, ist_seconds
from errand.domain.outcomes import (
    ActionFailed,
    AlreadyHandled,
    BookCalled,
    Booked,
    Requote,
    StepResult,
)
from errand.execution import intents as drafts
from errand.execution.executor import ConsequentialClient
from errand.store.agent_db import Repos
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.intents import IntentRow
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookError, BookOut, ListBookingsOut, hold_is_live

TERMINAL_INTENT = frozenset({"succeeded", "failed"})


async def execute(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    assert state.errand is not None and isinstance(state.inbound, ApprovalGranted)
    with deps.tracer.node("execute", phase_before=state.errand.phase.value) as span:
        run = _Execute(deps, state.errand, state.inbound.approval_id)
        try:
            result = await run.run()
        finally:
            if not isinstance(run.result, BookCalled):
                # Only an attempt handed to verify keeps the lock; verify releases it.
                await deps.release_held()
        span.end_fields["phase_after"] = run.errand.phase.value
    return {
        "errand": run.errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }


def route_after_execute(state: ThreadState) -> str:
    if isinstance(state.result, BookCalled):
        return "verify"
    if isinstance(state.result, Requote):
        return "prepare_proposal"
    return "respond"


class _Execute:
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
        row, swept = await service.live_approval(
            deps.db, deps.conversation_id, to_ms(now), deps.approval_policy
        )
        emit_swept(deps.tracer, swept)
        async with deps.db.session() as repos:
            row = await repos.approvals.get(self.approval_id)
        if row is None or row.status != "granted":
            status = row.status if row is not None else "missing"
            return AlreadyHandled(approval_id=self.approval_id, reason=status)
        approved = parse_material(row.material_json)
        assert isinstance(approved, BookMaterial)

        hold = self.errand.hold
        if hold is None or hold.hold_id != row.bound_hold_id:
            return await self._invalidate_and_requote(row, "material_mismatch", None)
        margin_s = deps.timeouts.book + deps.approval_settings.hold_margin_s
        if not hold_is_live(now + timedelta(seconds=margin_s), hold.expires_at):
            # M4 never books on a lapsed or nearly lapsed hold: a fresh hold and a new
            # card instead (M5 adds the re-hold that keeps an unchanged approval).
            return Requote(approval_id=self.approval_id, reason="hold_lapsed")

        try:
            listed = await deps.tools.call(
                Caller.EXECUTOR, "list_bookings", {"user_id": deps.user_id}, critical=True
            )
        except ToolError:
            return AlreadyHandled(approval_id=self.approval_id, reason="bookings_unavailable")
        if not isinstance(listed, ListBookingsOut):
            return AlreadyHandled(approval_id=self.approval_id, reason="bookings_unavailable")
        if duplicate_of(listed.bookings, approved.provider_id, hold.slot.start) is not None:
            await service.invalidate(deps.db, self.approval_id, "duplicate_risk")
            self._emit_invalidated(row, "duplicate_risk", None)
            self._move(Phase.COMPARING, "approval_invalidated")
            return ActionFailed(approval_id=self.approval_id, reason="duplicate")
        current = BookMaterial(
            user_id=deps.user_id,
            provider_id=hold.provider_id,
            service=approved.service,
            slot_start=ist_seconds(hold.slot.start),
            slot_end=ist_seconds(hold.slot.end),
            quoted_price_inr=hold.quoted_price_inr,
            same_service_nearby=[
                b.booking_id
                for b in nearby_bookings(listed.bookings, approved.service, hold.slot.start)
            ],
        )
        current_fp = fingerprint(current)
        if current_fp != row.fingerprint:
            reason = (
                "duplicate_risk"
                if current.same_service_nearby != approved.same_service_nearby
                else "material_mismatch"
            )
            return await self._invalidate_and_requote(row, reason, current_fp)

        intent_row = drafts.book_intent(
            approval_id=self.approval_id,
            conversation_id=deps.conversation_id,
            errand_id=self.errand.errand_id,
            user_id=deps.user_id,
            fingerprint=row.fingerprint,
            material_json=row.material_json,
            now_ms=to_ms(now),
        )
        attempt = drafts.first_book_attempt(
            intent_row,
            hold_id=hold.hold_id,
            hold_expires_at=to_ms(hold.expires_at),
            fence_epoch=held.epoch,
            now_ms=to_ms(now),
        )

        async def recheck(repos: Repos) -> str | None:
            # Inside the guard transaction: anything another conversation started since
            # the card was shown is visible here, after the user lock serialised us.
            for intent in await repos.intents.unresolved_for_user(deps.user_id):
                if intent.approval_id == self.approval_id:
                    # This approval's own intent: whether it may be consumed again is the
                    # consume's question (status = 'granted'), not a duplicate's.
                    continue
                material = parse_material(intent.material_json)
                if (
                    isinstance(material, BookMaterial)
                    and material.provider_id == current.provider_id
                    and material.slot_start == current.slot_start
                ):
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
                return ActionFailed(approval_id=self.approval_id, reason="duplicate")
            deps.tracer.emit(
                "guard.planner_tool_rejected",
                detail={"tool": "book", "reason": f"approval {token.status}: not consumable"},
            )
            return AlreadyHandled(approval_id=self.approval_id, reason=token.reason)

        tracer = deps.tracer
        tracer.emit(
            "approval.consumed",
            approval_id=self.approval_id,
            fingerprint=row.fingerprint,
            intent_id=intent_row.intent_id,
            hold_id=hold.hold_id,
        )
        tracer.emit("card.update", approval_id=self.approval_id, status="consumed")
        tracer.emit(
            "intent.written",
            intent_id=intent_row.intent_id,
            attempt=1,
            idempotency_key=attempt.idempotency_key,
            hold_id=hold.hold_id,
            state="prepared",
        )
        self._move(Phase.EXECUTING, "approval_granted")

        async with deps.db.transaction() as repos:
            moved = await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("prepared",),
                to="in_flight",
                fence_epoch=held.epoch,
                now_ms=to_ms(deps.clock.now()),
                add_calls=1,
            )
            sent = await repos.intents.get_attempt(attempt.attempt_id)
        assert moved and sent is not None, "the attempt was fenced off before it was sent"
        tracer.emit(
            "intent.in_flight",
            intent_id=intent_row.intent_id,
            attempt=1,
            idempotency_key=sent.idempotency_key,
            hold_id=sent.hold_id,
            state="in_flight",
        )
        client = ConsequentialClient(deps.tools)
        assert sent.hold_id is not None
        try:
            answer: BookOut | BookError | None = await client.book(
                token,
                intent_id=intent_row.intent_id,
                hold_id=sent.hold_id,
                idempotency_key=sent.idempotency_key,
            )
            error = None
        except ToolError as exc:
            answer, error = None, type(exc).__name__
        return await self._classify(intent_row, sent.attempt_id, held.epoch, answer, error)

    async def _classify(
        self,
        intent: IntentRow,
        attempt_id: str,
        epoch: int,
        answer: BookOut | BookError | None,
        error: str | None,
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        if isinstance(answer, BookError):
            # The world answered for this key: definitively not applied.
            async with deps.db.transaction() as repos:
                await repos.intents.move_attempt(
                    attempt_id,
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
                    result_json=f'{{"error":"{answer.error}"}}',
                    now_ms=now_ms,
                )
            deps.tracer.emit(
                "intent.resolved", intent_id=intent.intent_id, state="failed", error=answer.error
            )
            self._move(Phase.COMPARING, answer.error)
            reason: Literal["slot_taken", "hold_expired", "provider_refused"] = (
                "slot_taken"
                if answer.error == "slot_taken"
                else "hold_expired"
                if answer.error == "hold_expired"
                else "provider_refused"
            )
            return ActionFailed(
                approval_id=self.approval_id,
                reason=reason,
                fresh_quote=reason == "hold_expired",
            )
        # Everything else, a clean confirmation included, is settled by verify.
        last_error = error
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt_id,
                frm=("in_flight",),
                to="unknown",
                fence_epoch=epoch,
                now_ms=now_ms,
                last_error=last_error,
            )
        deps.tracer.emit(
            "intent.unknown",
            intent_id=intent.intent_id,
            attempt=1,
            state="unknown",
            error=last_error,
        )
        confirmed = isinstance(answer, BookOut)
        return BookCalled(
            approval_id=self.approval_id,
            intent_id=intent.intent_id,
            attempt_id=attempt_id,
            answer="confirmed" if confirmed else "unknown",
            booking_id=answer.booking_id if isinstance(answer, BookOut) else None,
            price_inr=answer.price_inr if isinstance(answer, BookOut) else None,
        )

    async def _reentry(self, intent: IntentRow) -> StepResult:
        deps = self.deps
        if intent.state == "succeeded" and intent.result_json:
            booking_id = str(json.loads(intent.result_json).get("booking_id", ""))
            return Booked(
                approval_id=self.approval_id, intent_id=intent.intent_id, booking_id=booking_id
            )
        if intent.state in TERMINAL_INTENT:
            return ActionFailed(approval_id=self.approval_id, reason="not_consumable")
        async with deps.db.session() as repos:
            attempts = await repos.intents.attempts(intent.intent_id)
        last = attempts[-1]
        if self.errand.phase is Phase.AWAITING_APPROVAL:
            self._move(Phase.EXECUTING, "approval_granted")
        return BookCalled(
            approval_id=self.approval_id,
            intent_id=intent.intent_id,
            attempt_id=last.attempt_id,
            answer="unknown",
        )

    async def _invalidate_and_requote(
        self, row: ApprovalRow, reason: str, new_fp: str | None
    ) -> StepResult:
        await service.invalidate(self.deps.db, row.approval_id, reason)
        self._emit_invalidated(row, reason, new_fp)
        return Requote(approval_id=row.approval_id, reason="material_changed")

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
