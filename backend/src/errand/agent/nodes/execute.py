"""execute: from a granted approval to one `book` call (architecture 8.4, 9.4). Code only.

Under the per-user consequential lock (a `user:<id>` lease plus an in-process lock,
held through `verify`):

1. Re-entry first: an intent for this approval means this approval was already acted
   on. A terminal intent returns its outcome; an unresolved one goes to `verify`. No
   new key is ever made for it.
2. Preflight, no side effects: the row is `granted` and within its grace; the bound
   hold is the errand's; a fresh `list_bookings` shows no exact duplicate. A hold that
   has lapsed, or has less than the book timeout plus `approvals.hold_margin_s` left, is
   held again (once per approval, read from agent.sqlite) and the fingerprint is
   recomputed from what the provider now says: equal means the approval moves to the new
   hold (`approval.hold_rebound`, no second card, S20); different invalidates it as
   `price_changed` or `slot_changed` and the new card is built on the new hold (S19, S50).
   Any other change invalidates it too, and the errand goes back to `prepare_proposal`
   for a new card (INV-12).
3. The guard transaction (approvals.service.consume): the duplicate re-check against
   current rows, the consume (exactly one row), the intent and attempt 1 as `prepared`
   with the hold's expiry, the idempotency key from `derive_idem_key` and `fence_epoch`
   from the user lease. Commit; only then is the ApprovalToken minted.
4. The attempt moves to `in_flight` (committed, so a `prepared` attempt is provably
   unsent), and `book` goes out under `asyncio.shield` with the key read back from the
   row. Any answer except a definitive refusal goes to `verify`: a clean `confirmed` is
   not trusted until `get_booking_status` agrees.
5. A refusal is the world's answer for this key: `slot_taken` closes the attempt
   (`not_applied`) and the intent (`failed`) and sends the errand back to COMPARING with
   one SystemNote replan (S25). `hold_expired` as the answer to the first send re-holds
   once, if the approval is within its TTL and no re-hold was used, and on an equal
   fingerprint sends attempt 2 on the new hold under a new key, the same intent; in every
   other case the intent fails and the user needs a fresh card.

A re-entry that finds the attempt still `prepared` closes it as never sent (M4 stopgap,
8.7; M6 owns sending it once with the same key). If the preflight cannot list the user's
bookings, nothing is consumed: the row stays `granted` (within its grace) and the user is
told to tap again, which restarts the run from the stored resume.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from langgraph.runtime import Runtime

from errand.agent import cards
from errand.agent.deps import RunDeps
from errand.agent.nodes.booking import BookingPath
from errand.agent.records import emit_swept
from errand.agent.state import ThreadState
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
    PendingCheck,
    Replan,
    Requote,
    StepResult,
)
from errand.domain.slots import HoldRef
from errand.domain.windows import slot_label
from errand.execution import intents as drafts
from errand.store.agent_db import Repos
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.intents import AttemptRow, IntentRow
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookingItem, ListBookingsOut, Slot, hold_is_live

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
    update: dict[str, Any] = {
        "errand": run.errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }
    if isinstance(result, Replan):
        update["plan_result"] = None
        update["system_notes"] = [*state.system_notes, *run.path.notes]
    return update


def route_after_execute(state: ThreadState) -> str:
    if isinstance(state.result, BookCalled):
        return "verify"
    if isinstance(state.result, Requote):
        return "prepare_proposal"
    if isinstance(state.result, Replan):
        return "plan"
    return "respond"


class _Execute:
    def __init__(self, deps: RunDeps, errand: Errand, approval_id: str) -> None:
        self.deps = deps
        self.approval_id = approval_id
        self.result: StepResult | None = None
        self.path = BookingPath(deps, errand)

    @property
    def errand(self) -> Errand:
        return self.path.errand

    def _move(self, to: Phase, reason: str) -> None:
        self.path.move(to, reason)

    async def run(self) -> StepResult:
        self.result = await self._run()
        return self.result

    # ------------------------------------------------------------------ preflight

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
        listed = await self._list_bookings()
        if listed is None:
            return AlreadyHandled(approval_id=self.approval_id, reason="bookings_unavailable")
        if duplicate_of(listed, approved.provider_id, hold.slot.start) is not None:
            await service.invalidate(deps.db, self.approval_id, "duplicate_risk")
            self._emit_invalidated(row, "duplicate_risk", None)
            self._move(Phase.COMPARING, "approval_invalidated")
            return ActionFailed(approval_id=self.approval_id, reason="duplicate")

        margin_s = deps.timeouts.book + deps.approval_settings.hold_margin_s
        rebound: HoldRef | None = None
        if not hold_is_live(now + timedelta(seconds=margin_s), hold.expires_at):
            # 9.4 step 2: hold again, once per approval, and compare what the provider
            # now says with what the user approved.
            if await self.path.rehold_used(row):
                return Requote(approval_id=self.approval_id, reason="hold_lapsed")
            rebound = await self.path.rehold(approved, hold)
            if rebound is None:
                return Requote(approval_id=self.approval_id, reason="hold_lapsed")
        effective = rebound or hold
        current = self._material(approved, effective, listed)
        current_fp = fingerprint(current)
        if current_fp != row.fingerprint:
            return await self._changed(row, approved, current, current_fp, rebound)
        if rebound is not None:
            await service.rebind_hold(
                deps.db,
                self.approval_id,
                old_hold_id=hold.hold_id,
                new_hold_id=rebound.hold_id,
            )
            deps.tracer.emit(
                "approval.hold_rebound",
                approval_id=self.approval_id,
                fingerprint=current_fp,
                hold_id=rebound.hold_id,
                old_hold_id=hold.hold_id,
            )
            self.path.errand = self.errand.model_copy(update={"hold": rebound})
        hold = effective

        now_ms = to_ms(now)
        intent_row = drafts.book_intent(
            approval_id=self.approval_id,
            conversation_id=deps.conversation_id,
            errand_id=self.errand.errand_id,
            user_id=deps.user_id,
            fingerprint=row.fingerprint,
            material_json=row.material_json,
            now_ms=now_ms,
        )
        attempt = drafts.first_book_attempt(
            intent_row,
            hold_id=hold.hold_id,
            hold_expires_at=to_ms(hold.expires_at),
            fence_epoch=held.epoch,
            now_ms=now_ms,
        )

        async def recheck(repos: Repos) -> str | None:
            # Inside the guard transaction: anything another conversation started since
            # the card was shown is visible here, after the user lock serialised us.
            for other in await repos.intents.unresolved_for_user(deps.user_id):
                if other.approval_id == self.approval_id:
                    # This approval's own intent: whether it may be consumed again is the
                    # consume's question (status = 'granted'), not a duplicate's.
                    continue
                material = parse_material(other.material_json)
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
            now_ms=now_ms,
            policy=deps.approval_policy,
            recheck=recheck,
            write=write,
        )
        if isinstance(token, service.ConsumeRefused):
            if token.invalidated:
                self._emit_invalidated(row, token.reason, None)
                self._move(Phase.COMPARING, "approval_invalidated")
                return ActionFailed(approval_id=self.approval_id, reason="duplicate")
            # An executor-side refusal (the row was no longer granted), not a planner one.
            deps.tracer.emit(
                "approval.consume_refused",
                approval_id=self.approval_id,
                reason=token.reason,
                status=token.status,
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
        return await self._send(token, row, approved, intent_row, attempt, held.epoch)

    # ------------------------------------------------------------------ sending

    async def _send(
        self,
        token: service.ApprovalToken,
        row: ApprovalRow,
        approved: BookMaterial,
        intent: IntentRow,
        prepared: AttemptRow,
        epoch: int,
    ) -> StepResult:
        """Move a `prepared` attempt to `in_flight` (committed), send `book`, settle."""
        deps = self.deps
        async with deps.db.transaction() as repos:
            moved = await repos.intents.move_attempt(
                prepared.attempt_id,
                frm=("prepared",),
                to="in_flight",
                fence_epoch=epoch,
                now_ms=to_ms(deps.clock.now()),
                add_calls=1,
            )
            sent_row = await repos.intents.get_attempt(prepared.attempt_id)
        if not moved or sent_row is None:
            # Another holder moved this attempt first (a stale epoch). Nothing was sent by
            # us: the attempt stays as that holder left it, and recovery (M6) decides.
            deps.tracer.emit(
                "approval.consume_refused",
                approval_id=self.approval_id,
                reason="attempt_fenced_off",
                status="consumed",
            )
            return PendingCheck(
                approval_id=self.approval_id, intent_id=intent.intent_id, reason="fenced_off"
            )
        deps.tracer.emit(
            "intent.in_flight",
            intent_id=intent.intent_id,
            attempt=sent_row.n,
            idempotency_key=sent_row.idempotency_key,
            hold_id=sent_row.hold_id,
            state="in_flight",
        )
        sent = await self.path.send(token, intent.intent_id, sent_row)
        settled = await self.path.settle(intent.intent_id, sent_row, sent, epoch=epoch)
        if isinstance(settled, BookCalled):
            return settled
        if settled == "hold_expired" and sent_row.n == 1:
            return await self._hold_expired(token, row, approved, intent, epoch)
        return await self.path.refused(intent.intent_id, self.approval_id, settled)

    async def _hold_expired(
        self,
        token: service.ApprovalToken,
        row: ApprovalRow,
        approved: BookMaterial,
        intent: IntentRow,
        epoch: int,
    ) -> StepResult:
        """9.4 step 5: the world said `hold_expired` to the first send. Hold again and, if
        nothing the user approved has changed, send attempt 2 on the new hold under a new
        key (attempt 1 is `not_applied`, the world answered). Only within the approval's
        TTL and only if no re-hold was used before; otherwise the intent fails and the
        user needs a fresh card."""
        deps = self.deps
        path = self.path
        # `row` was read before the consume; a preflight re-hold moved the bound hold since.
        async with deps.db.session() as repos:
            row = await repos.approvals.get(self.approval_id) or row
        within_ttl = to_ms(deps.clock.now()) - row.requested_at < deps.approval_policy.ttl_ms
        hold = self.errand.hold
        if not within_ttl or hold is None or await path.rehold_used(row):
            return await path.refused(intent.intent_id, self.approval_id, "hold_expired")
        fresh = await path.rehold(approved, hold)
        listed = await self._list_bookings()
        if fresh is None or listed is None:
            return await path.refused(intent.intent_id, self.approval_id, "hold_expired")
        current = self._material(approved, fresh, listed)
        current_fp = fingerprint(current)
        if current_fp != row.fingerprint:
            # The approval is already consumed, so there is no card to invalidate: the
            # intent fails and the user gets a new card on the hold we just placed.
            await path.fail_intent(intent.intent_id, "hold_expired")
            reason, detail = self._what_changed(approved, current)
            return Requote(approval_id=self.approval_id, reason=reason, hold=fresh, detail=detail)
        deps.tracer.emit(
            "approval.hold_rebound",
            approval_id=self.approval_id,
            fingerprint=current_fp,
            hold_id=fresh.hold_id,
            old_hold_id=hold.hold_id,
        )
        self.path.errand = self.errand.model_copy(update={"hold": fresh})
        second = drafts.book_attempt(
            intent,
            n=2,
            hold_id=fresh.hold_id,
            hold_expires_at=to_ms(fresh.expires_at),
            fence_epoch=epoch,
            now_ms=to_ms(deps.clock.now()),
        )
        async with deps.db.transaction() as repos:
            await repos.intents.insert_attempt(second)
        deps.tracer.emit(
            "intent.written",
            intent_id=intent.intent_id,
            attempt=2,
            idempotency_key=second.idempotency_key,
            hold_id=fresh.hold_id,
            state="prepared",
        )
        return await self._send(token, row, approved, intent, second, epoch)

    # ------------------------------------------------------------------ helpers

    async def _list_bookings(self) -> list[BookingItem] | None:
        deps = self.deps
        try:
            listed = await deps.tools.call(
                Caller.EXECUTOR, "list_bookings", {"user_id": deps.user_id}, critical=True
            )
        except ToolError:
            return None
        return listed.bookings if isinstance(listed, ListBookingsOut) else None

    def _material(
        self, approved: BookMaterial, hold: HoldRef, bookings: list[BookingItem]
    ) -> BookMaterial:
        """What the provider would book now, from the hold and a fresh list."""
        return BookMaterial(
            user_id=self.deps.user_id,
            provider_id=hold.provider_id,
            service=approved.service,
            slot_start=ist_seconds(hold.slot.start),
            slot_end=ist_seconds(hold.slot.end),
            quoted_price_inr=hold.quoted_price_inr,
            same_service_nearby=[
                b.booking_id for b in nearby_bookings(bookings, approved.service, hold.slot.start)
            ],
        )

    def _what_changed(self, approved: BookMaterial, current: BookMaterial) -> tuple[Any, str]:
        """The invalidation reason and the facts for the sentence that explains it."""
        tz = self.deps.tz

        def label(m: BookMaterial) -> str:
            slot = Slot(
                start=datetime.fromisoformat(m.slot_start), end=datetime.fromisoformat(m.slot_end)
            )
            return slot_label(slot, tz)

        if (current.slot_start, current.slot_end) != (approved.slot_start, approved.slot_end):
            return "slot_changed", f"{label(approved)} is now {label(current)}"
        if current.quoted_price_inr != approved.quoted_price_inr:
            return (
                "price_changed",
                f"{cards.rupees(approved.quoted_price_inr)} is now "
                f"{cards.rupees(current.quoted_price_inr)}",
            )
        return "material_changed", ""

    async def _changed(
        self,
        row: ApprovalRow,
        approved: BookMaterial,
        current: BookMaterial,
        current_fp: str,
        rebound: HoldRef | None,
    ) -> StepResult:
        """The fingerprint differs from the card's: invalidate and ask again (INV-12)."""
        reason, detail = self._what_changed(approved, current)
        if reason == "material_changed":
            why = (
                "duplicate_risk"
                if current.same_service_nearby != approved.same_service_nearby
                else "material_mismatch"
            )
            return await self._invalidate_and_requote(row, why, current_fp)
        await service.invalidate(self.deps.db, row.approval_id, reason)
        self._emit_invalidated(row, reason, current_fp)
        return Requote(approval_id=row.approval_id, reason=reason, hold=rebound, detail=detail)

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
        if last.state == "prepared":
            return await self._abandon_unsent(intent, last)
        if self.errand.phase is Phase.AWAITING_APPROVAL:
            self._move(Phase.EXECUTING, "approval_granted")
        return BookCalled(
            approval_id=self.approval_id,
            intent_id=intent.intent_id,
            attempt_id=last.attempt_id,
            answer="unknown",
        )

    async def _abandon_unsent(self, intent: IntentRow, attempt: Any) -> StepResult:
        """M4 stopgap (8.7): a `prepared` attempt is provably unsent (it moves to
        `in_flight` in its own committed step before `book` goes out). Architecture 9.4
        sends it once with the same key; that crash recovery is M6's. Until then it is
        closed, never left for the next state change to skip: the attempt is
        `not_applied` ("never_sent"), the intent `failed`, and the user is told nothing
        was booked and can ask again."""
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("prepared",),
                to="not_applied",
                fence_epoch=attempt.fence_epoch,
                now_ms=now_ms,
                last_error="never_sent",
            )
        await self.path.fail_intent(intent.intent_id, "never_sent")
        return ActionFailed(approval_id=self.approval_id, reason="never_sent")

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
