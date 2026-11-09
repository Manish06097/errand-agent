"""verify: the source of truth decides what `book` did (architecture 9.4 steps 6 and 7, G7).

`get_booking_status(key)` is called before anything is reported, even after a clean
`confirmed`. A pass is at most 4 checks with simulated backoff 0.5, 1, 2 and 4 s before
them. What a pass finds decides:

- `confirmed`: compared with the consumed approval's material, provider and slot always
  and the price whenever `book` answered with one (after a timeout it did not, and the
  record says `price_unverified`). A match makes the attempt `applied`, the intent
  `succeeded`, upserts `known_bookings`: COMPLETED, BOOKED_VERIFIED. A difference makes
  the intent `needs_attention` and tells the user exactly what the provider recorded (the
  slot and provider by name, the price). A check that could not be finished (the list of
  bookings unavailable) is not a difference: it is its own reason, `check_incomplete`,
  with no `mismatch` on the trace, the intent `needs_attention` and nothing called booked;
- `cancelled_by_provider`: the booking was applied and then cancelled. The attempt is
  `applied`, the intent `succeeded`, `known_bookings` is upserted, and in the same
  transaction a `followup_jobs` row (booking, `provider_cancel`, `detected`) is written.
  One code-templated message carries both facts. The notify policy, the rebook card and
  moving the job to `notified` are M14's;
- `not_found` after an ambiguous `book`, with the hold lapsed by the world clock: from
  then on a late request can only get `hold_expired`, so it is definitive. The attempt is
  `not_applied`, the intent `failed`, ACTION_FAILED with a fresh-quote action;
- `not_found` after a clean `confirmed` from `book` is a contradiction, not an ambiguity:
  no retry and no failure report, the same `check_incomplete` state for a person;
- `not_found`, hold live, after an ambiguous `book` (a timeout, a 5xx, a malformed answer,
  or an attempt found `in_flight` after a crash): the same-key retry (step 7) while the
  budget allows it, the provider declares it dedupes keys and the errand is not abandoned.
  One transaction sets `same_key_retries + 1`, the attempt `in_flight` and `calls + 1`, and
  commits; only then does `book` go out again, on the same hold with the same key. The
  answer is classified like the first one and never trusted alone: the pass runs again;
- `not_found`, hold live, no retry allowed: the wait rule. The attempt stays `unknown`,
  the intent `needs_attention`, PENDING_VERIFICATION naming when the hold lapses;
- the status unavailable after 4 checks: the same state, and no `book` is ever sent while
  the status is unknown. No time is promised.

The consequential lock taken in `execute` is released here, whatever happens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from langgraph.runtime import Runtime

from errand.agent import cards
from errand.agent.changes import verify as changes_verify
from errand.agent.deps import RunDeps
from errand.agent.nodes.booking import BookingPath
from errand.agent.state import ThreadState
from errand.approvals import service
from errand.approvals.fingerprint import parse_material
from errand.clock import from_ms, to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.material import BookMaterial
from errand.domain.messages import ActionCard
from errand.domain.outcomes import (
    ActionFailed,
    BookCalled,
    Booked,
    PendingCheck,
    Replan,
    StepResult,
)
from errand.domain.windows import slot_label
from errand.store.repos.intents import AttemptRow, IntentRow
from errand.store.repos.known_bookings import KnownBooking
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookingStatusOut, ListBookingsOut, hold_is_live

# Simulated seconds waited before each of the (at most) 4 checks of a pass.
BACKOFF_S = (0.5, 1.0, 2.0, 4.0)


@dataclass(frozen=True)
class Approved:
    """What the card the user approved called the provider and the slot."""

    provider_name: str
    slot_label: str


@dataclass
class Comparison:
    """What the provider recorded against what the user approved.

    `differences` are real, user-readable differences ("a price of ... instead of ...").
    `incomplete` means a read needed for the comparison failed, so the check could not be
    finished; it says nothing about the booking and is never a difference.
    """

    differences: list[str] = field(default_factory=list)
    incomplete: bool = False


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
    update: dict[str, Any] = {
        "errand": check.errand,
        "result": result,
        "counters": deps.counters.model_copy(deep=True),
    }
    if isinstance(result, Replan):
        update["plan_result"] = None
        update["system_notes"] = [*state.system_notes, *check.path.notes]
    return update


def route_after_verify(state: ThreadState) -> str:
    return "plan" if isinstance(state.result, Replan) else "respond"


class _Verify:
    def __init__(self, deps: RunDeps, errand: Errand, called: BookCalled) -> None:
        self.deps = deps
        self.called = called
        self.path = BookingPath(deps, errand)
        # Read from the record now, before this node moves the phase, so "Never mind"
        # (M6 adds the action) only has to set it.
        self.abandoned = errand.phase is Phase.ABANDONED

    @property
    def errand(self) -> Errand:
        return self.path.errand

    def _move(self, to: Phase, reason: str) -> None:
        self.path.move(to, reason)

    async def run(self) -> StepResult:
        deps = self.deps
        called = self.called
        async with deps.db.session() as repos:
            intent = await repos.intents.get(called.intent_id)
        assert intent is not None
        material = parse_material(intent.material_json)
        assert isinstance(material, BookMaterial)
        self._move(
            Phase.VERIFYING, "book_confirmed" if called.answer == "confirmed" else "book_unknown"
        )
        while True:
            async with deps.db.session() as repos:
                attempt = await repos.intents.get_attempt(self.called.attempt_id)
                intent = await repos.intents.get(called.intent_id) or intent
            assert attempt is not None
            key = attempt.idempotency_key
            status, checks = await self._status_pass(key)
            await self._count_checks(attempt, checks)
            hold_live = self._hold_live(attempt)
            if status is None:
                deps.tracer.emit(
                    "status.check",
                    idempotency_key=key,
                    result=None,
                    hold_live=hold_live,
                    decision="escalate",
                )
                return await self._unresolved(attempt, "status_unavailable", "")
            if status.status in ("confirmed", "cancelled_by_provider") and status.booking_id:
                return await self._found(attempt, intent, material, status, hold_live)
            if status.status != "not_found":
                deps.tracer.emit(
                    "status.check",
                    idempotency_key=key,
                    result=status.model_dump(mode="json"),
                    hold_live=hold_live,
                    decision="escalate",
                )
                return await self._unresolved(attempt, "material_mismatch", _status_phrase(status))
            if self.called.answer == "confirmed":
                # `book` said confirmed and the provider has no record under the key: a
                # contradiction, so nothing is retried and nothing is reported failed.
                deps.tracer.emit(
                    "status.check",
                    idempotency_key=key,
                    result=status.model_dump(mode="json"),
                    hold_live=hold_live,
                    decision="escalate",
                    check_incomplete=True,
                )
                return await self._unresolved(
                    attempt, "check_incomplete", "not_found_after_confirmed"
                )
            if hold_live is False:
                return await self._lapsed(attempt, intent, status)
            if attempt.state == "prepared":
                # Provably never sent (the `in_flight` commit comes before the call), and the
                # provider has no record of the key: this is the first send, not a retry.
                outcome = await self._send_prepared(attempt, intent, status)
                if isinstance(outcome, BookCalled):
                    self.called = outcome
                    continue
                return outcome
            if self._may_retry(intent):
                outcome = await self._retry(attempt, intent, status)
                if isinstance(outcome, BookCalled):
                    self.called = outcome
                    continue
                return outcome
            return await self._wait(attempt, status)

    # ------------------------------------------------------------------ status

    async def _status_pass(self, key: str) -> tuple[BookingStatusOut | None, int]:
        """Up to 4 checks, waiting 0.5, 1, 2 and 4 s (simulated) before them."""
        deps = self.deps
        checks = 0
        for delay in BACKOFF_S:
            await deps.clock.sleep(delay)
            checks += 1
            try:
                answer = await deps.tools.call(
                    Caller.VERIFIER, "get_booking_status", {"idempotency_key": key}, critical=True
                )
            except ToolError:
                continue
            return (answer if isinstance(answer, BookingStatusOut) else None), checks
        return None, checks

    async def _count_checks(self, attempt: AttemptRow, checks: int) -> None:
        deps = self.deps
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=(attempt.state,),
                to=attempt.state,
                fence_epoch=attempt.fence_epoch,
                now_ms=to_ms(deps.clock.now()),
                add_status_checks=checks,
            )

    def _hold_live(self, attempt: AttemptRow) -> bool | None:
        if attempt.hold_expires_at is None:
            return None
        return hold_is_live(self.deps.clock.now(), from_ms(attempt.hold_expires_at))

    def _may_retry(self, intent: IntentRow) -> bool:
        """Step 6 allows the same-key retry only after an ambiguous call."""
        s = self.deps.booking_settings
        return (
            self.called.answer == "unknown"
            and s.provider_dedupes_keys
            and intent.same_key_retries < s.max_same_key_retries
            and not self.abandoned
        )

    # ------------------------------------------------------------------ found

    async def _found(
        self,
        attempt: AttemptRow,
        intent: IntentRow,
        material: BookMaterial,
        status: BookingStatusOut,
        hold_live: bool | None,
    ) -> StepResult:
        deps = self.deps
        key = attempt.idempotency_key
        cancelled = status.status == "cancelled_by_provider"
        comparison = await self._compare(material, status, await self._approved())
        mismatch = comparison.differences
        # A real difference outranks an unfinished check: the user hears what differs.
        incomplete = comparison.incomplete and not mismatch
        price_unverified = self.called.price_inr is None
        decision: Literal["report_success", "report_provider_cancelled", "escalate"] = (
            "escalate"
            if mismatch or incomplete
            else "report_provider_cancelled"
            if cancelled
            else "report_success"
        )
        deps.tracer.emit(
            "status.check",
            idempotency_key=key,
            result=status.model_dump(mode="json"),
            hold_live=hold_live,
            decision=decision,
            price_unverified=price_unverified or None,
            mismatch=mismatch or None,
            check_incomplete=incomplete or None,
        )
        assert status.booking_id is not None
        if mismatch:
            return await self._unresolved(
                attempt,
                "material_mismatch",
                "; ".join(mismatch),
                applied=True,
                booking_id=status.booking_id,
            )
        if incomplete:
            return await self._unresolved(
                attempt,
                "check_incomplete",
                "list_bookings_unavailable",
                applied=True,
                booking_id=status.booking_id,
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
            )
            await repos.intents.resolve_intent(
                intent.intent_id,
                frm=("open", "needs_attention"),
                to="succeeded",
                result_json=json.dumps(
                    {
                        "booking_id": booking_id,
                        **({"provider_cancelled": True} if cancelled else {}),
                    }
                ),
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
                    status="cancelled_by_provider" if cancelled else "confirmed",
                    last_seen_at=now_ms,
                    origin_conversation_id=deps.conversation_id,
                    origin_brief_json=self.errand.brief.model_dump_json(),
                    source_intent_id=intent.intent_id,
                )
            )
            if cancelled:
                # Same transaction as the booking record: the unique (booking_id,
                # event_kind) index is what keeps the watcher (M14) from handling it twice.
                await repos.followups.detect(
                    job_id=f"fj_{booking_id}_provider_cancel",
                    booking_id=booking_id,
                    event_kind="provider_cancel",
                    conversation_id=deps.conversation_id,
                    now_ms=now_ms,
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
        self._move(
            Phase.COMPLETED, "status_cancelled_by_provider" if cancelled else "status_confirmed"
        )
        return Booked(
            approval_id=self.called.approval_id,
            intent_id=intent.intent_id,
            booking_id=booking_id,
            cancelled_by_provider=cancelled,
        )

    async def _approved(self) -> Approved | None:
        async with self.deps.db.session() as repos:
            row = await repos.approvals.get(self.called.approval_id)
        if row is None:
            return None
        card = ActionCard.model_validate_json(row.card_json)
        return Approved(card.provider_name, card.slot_label)

    async def _compare(
        self, material: BookMaterial, status: BookingStatusOut, card: Approved | None
    ) -> Comparison:
        """What the provider recorded that differs from what the user approved, in words the
        user can read: the slot and the provider by name, the price in rupees."""
        deps = self.deps
        called = self.called
        out = Comparison()
        if called.booking_id is not None and status.booking_id != called.booking_id:
            out.differences.append("a booking number that does not match the confirmation it gave")
        start = datetime.fromisoformat(material.slot_start)
        end = datetime.fromisoformat(material.slot_end)
        if status.slot is None or status.slot.start != start or status.slot.end != end:
            approved = f" instead of the {card.slot_label} you approved" if card else ""
            recorded = f" ({slot_label(status.slot, deps.tz)})" if status.slot else ""
            out.differences.append(f"a different slot{recorded}{approved}")
        if called.price_inr is not None and called.price_inr != material.quoted_price_inr:
            out.differences.append(
                f"a price of {cards.rupees(called.price_inr)} instead of the "
                f"{cards.rupees(material.quoted_price_inr)} you approved"
            )
        try:
            listed = await deps.tools.call(
                Caller.VERIFIER, "list_bookings", {"user_id": deps.user_id}, critical=True
            )
        except ToolError:
            listed = None
        if not isinstance(listed, ListBookingsOut):
            out.incomplete = True
            return out
        mine = next((b for b in listed.bookings if b.booking_id == status.booking_id), None)
        if mine is None:
            out.differences.append("a booking that is not in your list of bookings")
        elif mine.provider_id != material.provider_id:
            approved = (
                f" instead of {card.provider_name}, the provider you approved" if card else ""
            )
            out.differences.append(f"a booking with {mine.provider_name}{approved}")
        return out

    # ------------------------------------------------------------------ not found

    async def _lapsed(
        self, attempt: AttemptRow, intent: IntentRow, status: BookingStatusOut
    ) -> StepResult:
        """`not_found` after the hold lapsed by the world clock is definitive (9.2): a
        late request can only get `hold_expired` now. A fresh hold, a new card."""
        deps = self.deps
        self._trace_not_found(attempt, status, False, "report_not_applied")
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight"),
                to="not_applied",
                fence_epoch=attempt.fence_epoch,
                now_ms=to_ms(deps.clock.now()),
                last_error="not_found_after_lapse",
            )
        await self.path.fail_intent(intent.intent_id, "not_booked")
        return ActionFailed(
            approval_id=self.called.approval_id, reason="not_booked", fresh_quote=True
        )

    async def _retry(
        self, attempt: AttemptRow, intent: IntentRow, status: BookingStatusOut
    ) -> BookCalled | StepResult:
        """Step 7: one transaction, then `book` again on the same hold with the same key."""
        deps = self.deps
        self._trace_not_found(attempt, status, True, "same_key_retry")
        async with deps.db.transaction() as repos:
            moved = await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("unknown", "in_flight"),
                to="in_flight",
                fence_epoch=attempt.fence_epoch,
                now_ms=to_ms(deps.clock.now()),
                add_calls=1,
            )
            if moved:
                await repos.intents.bump_same_key_retries(
                    intent.intent_id, now_ms=to_ms(deps.clock.now())
                )
            sent_row = await repos.intents.get_attempt(attempt.attempt_id)
        if not moved or sent_row is None:
            return await self._unresolved(attempt, "fenced_off", "")
        deps.tracer.emit(
            "intent.retried",
            intent_id=intent.intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            hold_id=attempt.hold_id,
            state="in_flight",
            same_key_retries=intent.same_key_retries + 1,
            book_calls=sent_row.calls,
        )
        token = await service.token_for_consumed(
            deps.db, approval_id=self.called.approval_id, intent_id=intent.intent_id
        )
        if token is None:
            return await self._unresolved(sent_row, "status_unavailable", "no consumed approval")
        sent = await self.path.send(token, intent.intent_id, sent_row)
        settled = await self.path.settle(
            intent.intent_id, sent_row, sent, epoch=attempt.fence_epoch
        )
        if isinstance(settled, BookCalled):
            return settled
        return await self.path.refused(intent.intent_id, self.called.approval_id, settled)

    async def _send_prepared(
        self, attempt: AttemptRow, intent: IntentRow, status: BookingStatusOut
    ) -> BookCalled | StepResult:
        """9.4 step 6: a `prepared` attempt whose key the provider has never seen is sent
        once, with its own key, on its own hold (S36). Not a same-key retry: the budget
        is untouched. The `in_flight` move is committed before the call, as for any send."""
        deps = self.deps
        self._trace_not_found(attempt, status, True, "send_prepared")
        async with deps.db.transaction() as repos:
            moved = await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("prepared",),
                to="in_flight",
                fence_epoch=attempt.fence_epoch,
                now_ms=to_ms(deps.clock.now()),
                add_calls=1,
            )
            sent_row = await repos.intents.get_attempt(attempt.attempt_id)
        if not moved or sent_row is None:
            return await self._unresolved(attempt, "fenced_off", "")
        deps.tracer.emit(
            "intent.in_flight",
            intent_id=intent.intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            hold_id=attempt.hold_id,
            state="in_flight",
        )
        token = await service.token_for_consumed(
            deps.db, approval_id=self.called.approval_id, intent_id=intent.intent_id
        )
        if token is None:
            return await self._unresolved(sent_row, "status_unavailable", "no consumed approval")
        sent = await self.path.send(token, intent.intent_id, sent_row)
        settled = await self.path.settle(
            intent.intent_id, sent_row, sent, epoch=attempt.fence_epoch
        )
        if isinstance(settled, BookCalled):
            return settled
        return await self.path.refused(intent.intent_id, self.called.approval_id, settled)

    async def _wait(self, attempt: AttemptRow, status: BookingStatusOut) -> StepResult:
        """The wait rule: the request may still land, so nothing is reported as failed and
        nothing more is sent. The reconciler (M6) turns it definitive when the hold lapses."""
        self._trace_not_found(attempt, status, True, "wait_for_lapse")
        return await self._unresolved(
            attempt, "hold_live", "", lapse_at=self.path.lapse_at(attempt)
        )

    def _trace_not_found(
        self,
        attempt: AttemptRow,
        status: BookingStatusOut,
        hold_live: bool,
        decision: Literal[
            "same_key_retry", "report_not_applied", "wait_for_lapse", "send_prepared"
        ],
    ) -> None:
        self.deps.tracer.emit(
            "status.check",
            idempotency_key=attempt.idempotency_key,
            result=status.model_dump(mode="json"),
            hold_live=hold_live,
            decision=decision,
        )

    # ------------------------------------------------------------------ unresolved

    async def _unresolved(
        self,
        attempt: AttemptRow,
        reason: Literal[
            "status_unavailable", "material_mismatch", "check_incomplete", "hold_live", "fenced_off"
        ],
        detail: str,
        *,
        applied: bool = False,
        lapse_at: str | None = None,
        booking_id: str | None = None,
    ) -> StepResult:
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        async with deps.db.transaction() as repos:
            if applied:
                await repos.intents.move_attempt(
                    attempt.attempt_id,
                    frm=("unknown", "in_flight"),
                    to="applied",
                    fence_epoch=attempt.fence_epoch,
                    now_ms=now_ms,
                )
            await repos.intents.resolve_intent(
                attempt.intent_id,
                frm=("open", "needs_attention"),
                to="needs_attention",
                # booking_id is set when the provider did book something (applied): M9's
                # cancel card for a booking that differs reads it from here.
                result_json=json.dumps(
                    {
                        "reason": reason,
                        "detail": detail,
                        **({"booking_id": booking_id} if booking_id else {}),
                    }
                ),
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
            lapse_at=lapse_at,
            booking_id=booking_id,
        )


def _status_phrase(status: BookingStatusOut) -> str:
    """What the provider recorded when its answer is neither a booking nor `not_found`."""
    if status.status == "confirmed":
        return "a confirmation without a booking number"
    # Not "cancelled": the claim guard and INV-04 read that word as a cancel claim.
    return "the booking as no longer active"
