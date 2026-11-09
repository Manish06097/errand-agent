"""The booking path's shared steps, used by `execute` and `verify` (architecture 9.4).

`execute` owns the preflight, the guard transaction and the first send; `verify` owns the
status pass, the same-key retry and the final classification. Both need the same four
things, so they live here once:

- the re-hold (step 2, and step 5's `hold_expired` branch): one `hold_slot` for the
  approved slot, recorded as the next generation of that slot's holds, and the question
  "was this approval's automatic re-hold already used" answered from agent.sqlite
  (`approvals.bound_hold_id` against the hold the card was issued on), never from
  checkpoint state;
- sending `book` for an attempt that is already `in_flight` (committed before the call),
  through the token-checked `ConsequentialClient` under `asyncio.shield`;
- settling what came back: a world refusal makes the attempt `not_applied`; anything
  else (a clean confirmation included) makes it `unknown`, and `verify` decides;
- closing a failed intent (`slot_taken`, `hold_expired`, a refusal, a lapsed hold).

Code only. Nothing here reads model text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from errand.agent.deps import RunDeps
from errand.agent.state import SystemNote
from errand.agent.transitions import transition
from errand.approvals import service
from errand.clock import from_ms, to_ms
from errand.domain.errand import Errand, Phase
from errand.domain.material import BookMaterial
from errand.domain.messages import ActionCard
from errand.domain.outcomes import ActionFailed, BookCalled, Replan, StepResult
from errand.domain.slots import HoldRef
from errand.execution.executor import ConsequentialClient
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.holds import HoldRow, hold_record_id_for
from errand.store.repos.intents import AttemptRow, IntentRow
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import BookError, BookOut, HoldSlotOut, Slot

REPLANS_PER_RUN = 1
SLOT_TAKEN_NOTE = (
    "The provider says the slot {slot} with {provider} was just taken by someone else, so "
    "nothing was booked and the approval is spent. Search again and show the user other "
    "options; do not propose a booking until they choose one."
)


@dataclass(frozen=True)
class Sent:
    """What one `book` call came back with: an answer, or the transport error's class."""

    answer: BookOut | BookError | None
    error: str | None


@dataclass
class BookingPath:
    deps: RunDeps
    errand: Errand
    notes: list[SystemNote] = field(default_factory=list)

    def move(self, to: Phase, reason: str) -> None:
        self.errand = transition(self.errand, to, reason, self.deps.tracer)

    # ------------------------------------------------------------------ re-hold

    async def rehold(self, approved: BookMaterial, hold: HoldRef) -> HoldRef | None:
        """Hold the approved slot again. The provider may return the same window or a
        moved one (`faults.rehold_slot_shift`); the caller compares fingerprints."""
        deps = self.deps
        slot = Slot(
            start=datetime.fromisoformat(approved.slot_start),
            end=datetime.fromisoformat(approved.slot_end),
        )
        try:
            answer = await deps.tools.call(
                Caller.EXECUTOR,
                "hold_slot",
                {"provider_id": approved.provider_id, "slot": slot},
                critical=True,
            )
        except ToolError:
            return None
        if not isinstance(answer, HoldSlotOut):
            return None
        start_ms = to_ms(slot.start)
        async with deps.db.session() as repos:
            latest = await repos.holds.latest(self.errand.errand_id, approved.provider_id, start_ms)
            generation = latest.generation + 1 if latest is not None else 0
            row = await repos.holds.insert(
                HoldRow(
                    hold_record_id=hold_record_id_for(
                        self.errand.errand_id, approved.provider_id, start_ms, generation
                    ),
                    errand_id=self.errand.errand_id,
                    provider_id=approved.provider_id,
                    slot_start=start_ms,
                    generation=generation,
                    hold_id=answer.hold_id,
                    quoted_price_inr=answer.quoted_price_inr,
                    expires_at=to_ms(answer.expires_at),
                )
            )
        return HoldRef(
            hold_record_id=row.hold_record_id,
            hold_id=answer.hold_id,
            provider_id=approved.provider_id,
            slot=answer.slot,
            search_price_inr=hold.search_price_inr,
            quoted_price_inr=answer.quoted_price_inr,
            expires_at=answer.expires_at,
        )

    async def rehold_used(self, row: ApprovalRow) -> bool:
        """Whether this approval was already moved to a replacement hold.

        Read from agent.sqlite: the card records the expiry of the hold it was issued on,
        and `approvals.bound_hold_id` is the hold it is bound to now. A re-hold changes
        the second and leaves the first, so a different expiry means the one automatic
        re-hold per approval is spent.
        """
        if row.bound_hold_id is None:
            return False
        card = ActionCard.model_validate_json(row.card_json)
        async with self.deps.db.session() as repos:
            bound = await repos.holds.get_by_hold_id(row.bound_hold_id)
        issued = to_ms(datetime.fromisoformat(card.hold_expires_at))
        return bound is not None and bound.expires_at != issued

    # ------------------------------------------------------------------ sending

    async def send(self, token: service.ApprovalToken, intent_id: str, attempt: AttemptRow) -> Sent:
        """`book` for an attempt whose `in_flight` move is already committed."""
        assert attempt.hold_id is not None
        client = ConsequentialClient(self.deps.tools)
        try:
            answer = await client.book(
                token,
                intent_id=intent_id,
                hold_id=attempt.hold_id,
                idempotency_key=attempt.idempotency_key,
            )
        except ToolError as exc:
            return Sent(None, type(exc).__name__)
        return Sent(answer, None)

    async def settle(
        self, intent_id: str, attempt: AttemptRow, sent: Sent, *, epoch: int
    ) -> str | BookCalled:
        """Record what `book` came back with.

        A world refusal (`slot_taken`, `hold_expired`, `invalid_args`, key reuse) is the
        provider's answer for this key: the attempt is `not_applied`, and the error's name
        is returned for the caller to act on. Everything else, a clean `confirmed`
        included, leaves the attempt `unknown` and goes to `verify` (never trusted alone).
        """
        deps = self.deps
        now_ms = to_ms(deps.clock.now())
        if isinstance(sent.answer, BookError):
            async with deps.db.transaction() as repos:
                await repos.intents.move_attempt(
                    attempt.attempt_id,
                    frm=("in_flight",),
                    to="not_applied",
                    fence_epoch=epoch,
                    now_ms=now_ms,
                    last_error=sent.answer.error,
                )
            return sent.answer.error
        async with deps.db.transaction() as repos:
            await repos.intents.move_attempt(
                attempt.attempt_id,
                frm=("in_flight",),
                to="unknown",
                fence_epoch=epoch,
                now_ms=now_ms,
                last_error=sent.error,
            )
        deps.tracer.emit(
            "intent.unknown",
            intent_id=intent_id,
            attempt=attempt.n,
            idempotency_key=attempt.idempotency_key,
            state="unknown",
            error=sent.error,
        )
        answer = sent.answer
        intent = await self._intent(intent_id)
        return BookCalled(
            approval_id=intent.approval_id,
            intent_id=intent_id,
            attempt_id=attempt.attempt_id,
            answer="confirmed" if isinstance(answer, BookOut) else "unknown",
            booking_id=answer.booking_id if isinstance(answer, BookOut) else None,
            price_inr=answer.price_inr if isinstance(answer, BookOut) else None,
        )

    async def _intent(self, intent_id: str) -> IntentRow:
        async with self.deps.db.session() as repos:
            intent = await repos.intents.get(intent_id)
        assert intent is not None
        return intent

    # ------------------------------------------------------------------ closing

    async def fail_intent(self, intent_id: str, error: str) -> None:
        """The intent is over: failed, nothing was booked."""
        deps = self.deps
        async with deps.db.transaction() as repos:
            await repos.intents.resolve_intent(
                intent_id,
                frm=("open", "needs_attention"),
                to="failed",
                result_json=json.dumps({"error": error}),
                now_ms=to_ms(deps.clock.now()),
            )
        deps.tracer.emit("intent.resolved", intent_id=intent_id, state="failed", error=error)
        self.move(Phase.COMPARING, error)

    async def refused(self, intent_id: str, approval_id: str, error: str) -> StepResult:
        """Close the intent after the world's refusal and say what the user should do."""
        await self.fail_intent(intent_id, error)
        if error == "slot_taken":
            return await self.slot_taken(approval_id)
        if error == "hold_expired":
            return ActionFailed(approval_id=approval_id, reason="hold_expired", fresh_quote=True)
        return ActionFailed(approval_id=approval_id, reason="provider_refused")

    async def slot_taken(self, approval_id: str) -> StepResult:
        """The errand is back to COMPARING; one SystemNote replan per run (S25), else the
        user is told and picks from the options already on screen."""
        deps = self.deps
        if deps.counters.replans >= REPLANS_PER_RUN:
            return ActionFailed(approval_id=approval_id, reason="slot_taken")
        async with deps.db.session() as repos:
            row = await repos.approvals.get(approval_id)
        card = ActionCard.model_validate_json(row.card_json) if row is not None else None
        slot = card.slot_label if card is not None else "you chose"
        provider = card.provider_name if card is not None else "that provider"
        deps.counters.replans += 1
        self.notes.append(
            SystemNote(kind="slot_taken", text=SLOT_TAKEN_NOTE.format(slot=slot, provider=provider))
        )
        return Replan(note="slot_taken")

    def lapse_at(self, attempt: AttemptRow) -> str | None:
        if attempt.hold_expires_at is None:
            return None
        return from_ms(attempt.hold_expires_at).isoformat()
