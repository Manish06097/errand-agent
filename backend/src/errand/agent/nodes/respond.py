"""Respond: turns the run's result into code-rendered messages, the outcome, and parking.

Messages are written with an upsert keyed on (run_id, kind), so respond running again
after a crash rewrites the same rows instead of posting twice (6.5). They are written
in reading order, so the last one written is the last one the user sees.

What respond adds in M4:
- outcome templates for the booking path (AWAITING_APPROVAL, DECLINED, BOOKED_VERIFIED,
  ACTION_FAILED, PENDING_VERIFICATION and the code answers around them);
- the claim guard over every piece of model text (a reply, a shortlist recommendation,
  a question): a sentence no record backs is removed and `guard.claim_rewritten` is
  emitted;
- a code status line on the run's last message whenever the errand is not completed;
- parking: when a live *pending* approval exists after the sweep (a `granted` row whose
  run could not preflight, `bookings_unavailable`, is not parked on: the user is told to tap
  again and the stored resume restarts it), the run ends at
  `await_approval` with the card, or a code-rendered reminder of it, as the last
  assistant message. The reminder has `typed_grant_ok=false` if this run's model text
  holds a question mark or the run asked the user anything (8.3). A run that ends
  parked has outcome AWAITING_APPROVAL unless an ask, limit or error outcome wins.

One message per (run_id, kind) (migration 0002): a run that already posted a card (a
shortlist, a question card) and must still end with a reminder writes the reminder in
the notice slot. No M4 run needs a third message of the same kind.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from errand.agent import cards, critical, invalidation, recovery
from errand.agent.changes import render as changes_render
from errand.agent.claim_guard import BOOKING_ID, guard
from errand.agent.deps import RunDeps
from errand.agent.records import live_approval, verified_records
from errand.agent.state import CardChange, ThreadState
from errand.agent.templates import (
    NOT_BOOKED_TEXT,
    SLOT_TAKEN_TEXT,
    internal_error,
    provider_cancelled_text,
    render,
    requote_text,
    shortlist_card,
)
from errand.clock import to_ms
from errand.domain.brief import BriefField
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import Recover, UserMessage
from errand.domain.messages import (
    KIND_OF,
    ActionCard,
    CardReminder,
    MessageBody,
    NoticeAction,
    OutcomeNotice,
)
from errand.domain.outcomes import (
    ActionFailed,
    AlreadyHandled,
    Ask,
    Booked,
    CardReady,
    Changed,
    ChangeFailed,
    ConfirmHint,
    Declined,
    HoldFailed,
    NoAction,
    Options,
    OverBudget,
    PendingCheck,
    PlanResult,
    ProposalRefused,
    Reply,
    RunOutcome,
    StepResult,
)
from errand.ids import message_id_for
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.messages import Message

# An outcome that says something the user must know wins over AWAITING_APPROVAL.
_PRECEDENCE = frozenset(
    {
        RunOutcome.ASKED_USER,
        RunOutcome.LIMIT_REACHED,
        RunOutcome.WALL_CLOCK_EXCEEDED,
        RunOutcome.CONTEXT_TOO_LARGE,
        RunOutcome.CONTEXT_EXHAUSTED,
        RunOutcome.TOOL_UNAVAILABLE,
        RunOutcome.MODEL_UNAVAILABLE,
        RunOutcome.MODEL_NO_PROGRESS,
        RunOutcome.MODEL_REFUSED,
        RunOutcome.RUN_INTERRUPTED,
        RunOutcome.INTERNAL_ERROR,
        RunOutcome.PENDING_VERIFICATION,
    }
)
GUARDED_EMPTY = "Nothing has been booked yet."


async def respond(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    phase = state.errand.phase.value if state.errand else None
    with deps.tracer.node("respond", phase_before=phase) as span:
        r = _Respond(deps, state)
        outcome = await r.run()
        span.end_fields["phase_after"] = r.errand.phase.value if r.errand else None
    update: dict[str, Any] = {"outcome": outcome, "park": r.park}
    if r.errand is not None:
        update["errand"] = r.errand
    return update


def route_after_respond(state: ThreadState) -> str:
    return "await_approval" if state.park else "__end__"


class _Respond:
    def __init__(self, deps: RunDeps, state: ThreadState) -> None:
        self.deps = deps
        self.state = state
        self.errand: Errand | None = state.errand
        self.park: str | None = None
        self.model_text: list[str] = []
        self.asked = False
        # A verified booking this run's reply cites by id (guard-approved), for the status line.
        self.cited_booking: str | None = None
        self.cited_service: str | None = None

    # ------------------------------------------------------------------ main

    async def run(self) -> RunOutcome:
        deps = self.deps
        # An exit with an open intent is PENDING_VERIFICATION whatever else it was (15.1).
        left_open = await critical.exit_critical_section(
            deps.db,
            conversation_id=deps.conversation_id,
            intent_ids=deps.intents,
            now_ms=to_ms(deps.clock.now()),
            tracer=deps.tracer,
            reason="limit",
        )
        if left_open is not None:
            if not isinstance(self.state.result, PendingCheck):
                self.state = self.state.model_copy(update={"result": left_open})
            if self.errand is not None and self.errand.phase is Phase.EXECUTING:
                self.errand = self.errand.model_copy(update={"phase": Phase.NEEDS_ATTENTION})
        live = await live_approval(deps)
        outcome, bodies = await self._render(live)
        pending = live if live is not None and live.status == "pending" else None
        typed_ok = not self.asked and not any("?" in t for t in self.model_text)
        if pending is not None:
            last = bodies[-1] if bodies else None
            if not (isinstance(last, ActionCard) and last.approval_id == pending.approval_id):
                card = ActionCard.model_validate_json(pending.card_json)
                bodies.append(cards.reminder(card, typed_grant_ok=typed_ok))
            self.park = pending.approval_id
            if self.errand is not None:
                self.errand = self.errand.model_copy(update={"approval_id": pending.approval_id})
            if outcome not in _PRECEDENCE:
                outcome = RunOutcome.AWAITING_APPROVAL
        if bodies and self.errand is not None and self.errand.phase is not Phase.COMPLETED:
            last = bodies[-1]
            if getattr(last, "status_line", None) is None:
                bodies[-1] = last.model_copy(update={"status_line": self._status_line()})
        await self._write(bodies, typed_ok)
        return outcome

    def _status_line(self) -> str | None:
        """The code status line. A fresh errand that has taken no action, whose reply cites
        a verified booking ("That's already booked: B-5715 ..."), says so instead of
        "Nothing booked for this request yet" under it (8.7)."""
        assert self.errand is not None
        phase = self.errand.phase
        if self.state.card_changes:
            return _card_change_line(self.state.card_changes[-1])
        if (
            self.cited_booking
            and phase in (Phase.GATHERING, Phase.COMPARING)
            and self._cited_is_ours()
        ):
            return f"Booked: {self.cited_booking}, confirmed with the provider."
        return cards.status_line(phase, self.errand.kind)

    def _cited_is_ours(self) -> bool:
        """The cited booking belongs to this errand: same service as its brief, or the brief
        names no service yet (a bare "book it" follow-up, S29, has nothing it could
        contradict). A plumber booking never turns an electrician errand's status line
        into "Booked" (8.7)."""
        assert self.errand is not None
        service = self.errand.brief.service
        return service is None or str(service.value) == self.cited_service

    # ------------------------------------------------------------------ rendering

    async def _render(self, live: ApprovalRow | None) -> tuple[RunOutcome, list[MessageBody]]:
        result = self.state.result
        if result is not None:
            return await self._render_step(result, live)
        plan = await self._guarded(self.state.plan_result)
        if isinstance(plan, NoAction):
            return self._no_action()
        outcome, body = render(plan, self.errand, self.deps.run_id, self.deps.tz)
        if outcome is RunOutcome.ASKED_USER:
            self.asked = True
        if any(n.kind == "slot_taken" for n in self.state.system_notes):
            # The replan after a refused booking (S25): the user hears first, in code, that
            # nothing was booked, and then what the planner found instead.
            if isinstance(body, OutcomeNotice):
                return outcome, [body.model_copy(update={"text": f"{SLOT_TAKEN_TEXT} {body.text}"})]
            first = OutcomeNotice(outcome=outcome, text=SLOT_TAKEN_TEXT, actions=[])
            return outcome, [first, body]
        return outcome, [body]

    def _no_action(self) -> tuple[RunOutcome, list[MessageBody]]:
        """Twice prose about booking and no proposal (10.4): the options stay on screen."""
        bodies: list[MessageBody] = []
        shortlist = self.errand.shortlist if self.errand else None
        current = (
            self.errand is not None
            and shortlist is not None
            and shortlist.brief_version == self.errand.brief.version
            and bool(shortlist.candidates)
        )
        if current and self.errand is not None:
            bodies.append(shortlist_card(self.errand, None, self.deps.tz))
        bodies.append(
            OutcomeNotice(
                outcome=RunOutcome.MODEL_NO_PROGRESS,
                text=(
                    "I couldn't turn that into a booking, so nothing was booked. "
                    "Pick a time on the options, or tell me which one you want."
                ),
                actions=[NoticeAction(kind="options", label="See options")],
            )
        )
        return RunOutcome.MODEL_NO_PROGRESS, bodies

    async def _guarded(self, plan: PlanResult | None) -> PlanResult | None:
        """The claim guard over every piece of model text this result carries."""
        if isinstance(plan, Reply):
            text = await self._guard_text(plan.text, cites=True) or GUARDED_EMPTY
            self.model_text.append(text)
            return plan.model_copy(update={"text": text})
        if isinstance(plan, Options) and plan.recommendation:
            text = await self._guard_text(plan.recommendation)
            self.model_text.append(text)
            return plan.model_copy(update={"recommendation": text or None})
        if isinstance(plan, Ask):
            text = await self._guard_text(plan.question) or "Could you tell me a bit more?"
            self.model_text.append(text)
            return plan.model_copy(update={"question": text})
        return plan

    async def _guard_text(self, text: str, *, cites: bool = False) -> str:
        deps = self.deps
        records = await verified_records(deps, self._verified_this_run())
        names: dict[str, str] = {}
        shortlist = self.errand.shortlist if self.errand else None
        if shortlist is not None:
            names = {c.provider_id: c.name for c in shortlist.candidates}
        # The providers of the user's known bookings are names too (a modal may bridge
        # them); the shortlist's name for a provider wins.
        for record in records:
            names.setdefault(record.provider_id, record.provider_name)
        guarded = guard(text, records, deps.tz, provider_names=names)
        if guarded.removed:
            deps.tracer.emit(
                "guard.claim_rewritten",
                detail={"removed": guarded.removed, "kept": guarded.text},
            )
        if cites:
            known = {r.booking_id for r in records}
            ids = [i for i in dict.fromkeys(BOOKING_ID.findall(guarded.text)) if i in known]
            if len(ids) == 1:
                self.cited_booking = ids[0]
                self.cited_service = next(r.service for r in records if r.booking_id == ids[0])
        return guarded.text

    def _verified_this_run(self) -> set[str]:
        result = self.state.result
        return {result.booking_id} if isinstance(result, Booked | Changed) else set()

    async def _card_of(self, approval_id: str) -> ActionCard | None:
        async with self.deps.db.session() as repos:
            row = await repos.approvals.get(approval_id)
        return ActionCard.model_validate_json(row.card_json) if row is not None else None

    async def _render_step(
        self, result: StepResult, live: ApprovalRow | None
    ) -> tuple[RunOutcome, list[MessageBody]]:
        def notice(outcome: RunOutcome, text: str, *actions: NoticeAction) -> OutcomeNotice:
            return OutcomeNotice(outcome=outcome, text=text, actions=list(actions))

        if isinstance(result, CardReady):
            card = await self._card_of(result.approval_id)
            assert card is not None
            bodies: list[MessageBody] = []
            line = requote_text(result.replaced_reason, result.replaced_detail)
            if line is not None:
                bodies.append(notice(RunOutcome.AWAITING_APPROVAL, line))
            return RunOutcome.AWAITING_APPROVAL, [*bodies, card]
        if isinstance(result, ConfirmHint):
            card = await self._card_of(result.approval_id)
            assert card is not None
            reminder = cards.reminder(card, typed_grant_ok=True)
            hinted = reminder.model_copy(update={"hint": cards.confirm_hint(card)})
            return RunOutcome.AWAITING_APPROVAL, [hinted]
        if isinstance(result, Declined):
            return RunOutcome.DECLINED, [
                notice(
                    RunOutcome.DECLINED,
                    "Okay, I won't book that. Nothing was booked; the other options are still "
                    "here.",
                    NoticeAction(kind="options", label="See options"),
                )
            ]
        if isinstance(result, Changed):
            inbound = self.state.inbound
            return await changes_render.render_changed(
                self.deps,
                result,
                after_restart=isinstance(inbound, Recover) and inbound.after_restart,
            )
        if isinstance(result, ChangeFailed):
            return changes_render.render_failed(result)
        if isinstance(result, Booked):
            card = await self._card_of(result.approval_id)
            assert card is not None
            if result.cancelled_by_provider:
                # No status answer said `confirmed`, so this is not a *_VERIFIED outcome (INV-04).
                text = provider_cancelled_text(card.provider_name, card.slot_label)
                return RunOutcome.NOTIFIED, [notice(RunOutcome.NOTIFIED, text)]
            inbound = self.state.inbound
            after_restart = isinstance(inbound, Recover) and inbound.after_restart
            return RunOutcome.BOOKED_VERIFIED, [
                cards.booked_card(card, result.booking_id, after_restart=after_restart)
            ]
        if isinstance(result, ActionFailed):
            return RunOutcome.ACTION_FAILED, [_failed(result)]
        if isinstance(result, PendingCheck):
            pending = critical.pending_notice(result, self.deps.tz)
            return RunOutcome.PENDING_VERIFICATION, [pending]
        if isinstance(result, OverBudget):
            self.asked = True
            return RunOutcome.ASKED_USER, [
                notice(
                    RunOutcome.ASKED_USER,
                    f"The held quote from {result.provider_name} for {result.slot_label} is "
                    f"{cards.rupees(result.quoted_price_inr)}, above your budget of "
                    f"{cards.rupees(result.budget_inr)}, so I can't put it up for approval. "
                    f"Do you want to raise your budget to "
                    f"{cards.rupees(result.quoted_price_inr)}, or look at other options?",
                    NoticeAction(kind="raise_budget", label="Raise my budget"),
                    NoticeAction(kind="options", label="Other options"),
                )
            ]
        if isinstance(result, HoldFailed):
            return RunOutcome.REPLIED, [
                notice(
                    RunOutcome.REPLIED,
                    f"I couldn't hold {result.slot_label} with that provider, so nothing was "
                    "booked. Pick another slot from the options.",
                    NoticeAction(kind="options", label="See options"),
                )
            ]
        if isinstance(result, ProposalRefused):
            return RunOutcome.REPLIED, [notice(RunOutcome.REPLIED, result.text)]
        if isinstance(result, AlreadyHandled):
            if result.reason == "bookings_unavailable":
                text = (
                    "I couldn't check your bookings just now; nothing was booked. "
                    "Tap the button again to retry."
                )
            elif result.reason == recovery.NOTHING_TO_CHECK:
                text = "There is nothing left to check: that booking is already settled."
            elif result.reason in ("consumed", "not_consumable"):
                text = "Already approved: that booking is in progress or done."
            else:
                text = "That card can't be used any more, so nothing was booked."
            return RunOutcome.REPLIED, [notice(RunOutcome.REPLIED, text)]
        return RunOutcome.INTERNAL_ERROR, [internal_error(self.deps.run_id)]

    # ------------------------------------------------------------------ writing

    async def _write(self, bodies: list[MessageBody], typed_ok: bool) -> None:
        deps = self.deps
        inbound = self.state.inbound
        turn_no = inbound.turn_no if isinstance(inbound, UserMessage) else None
        now_ms = to_ms(deps.clock.now())
        used: set[str] = set()
        written: list[tuple[Message, MessageBody]] = []
        for body in bodies:
            kind = KIND_OF[body.type]
            if isinstance(body, CardReminder) and kind in used:
                kind = "notice"
            if kind in used:
                raise RuntimeError(f"respond: a second {kind} message in run {deps.run_id}")
            used.add(kind)
            approval_id = getattr(body, "approval_id", None)
            grant_ok: bool | None = None
            if isinstance(body, ActionCard):
                grant_ok = typed_ok
            elif isinstance(body, CardReminder):
                grant_ok = body.typed_grant_ok
            written.append(
                (
                    Message(
                        message_id=message_id_for(deps.run_id, kind),
                        conversation_id=deps.conversation_id,
                        client_msg_id=None,
                        role="assistant",
                        kind=kind,
                        body=body.model_dump(mode="json"),
                        run_id=deps.run_id,
                        turn_no=turn_no,
                        approval_id=approval_id,
                        typed_grant_ok=grant_ok,
                        rendered_at=None,
                        created_at=now_ms,
                    ),
                    body,
                )
            )
        async with deps.db.session() as repos:
            for message, _ in written:
                await repos.messages.upsert_assistant(message)
            await repos.conversations.touch(deps.conversation_id, now_ms)
        for message, body in written:
            if body.type in ("action", "reminder", "outcome"):
                deps.tracer.emit(
                    "card",
                    approval_id=message.approval_id,
                    message_id=message.message_id,
                    body={**message.body, "typed_grant_ok": message.typed_grant_ok},
                )


def _card_change_line(change: CardChange) -> str:
    """The status line after a correction touched the card on screen (8.5)."""
    if change.outcome == "invalidated":
        words = invalidation.changed_words([BriefField(f) for f in change.changed_fields])
        return f"This card is out of date: you changed {words}. Nothing was booked."
    if change.status == "consumed":
        return (
            "You had already approved the earlier card, so that booking goes ahead as shown; "
            "your change applies to anything after it."
        )
    return (
        f"The earlier card was already {change.status or 'closed'}, "
        "so your change did not affect it."
    )


def _failed(result: ActionFailed) -> OutcomeNotice:
    actions: list[NoticeAction] = []
    if result.reason == "slot_taken":
        text = (
            "The provider says that slot was just taken by someone else, so nothing was "
            "booked. Pick another slot from the options."
        )
        actions.append(NoticeAction(kind="options", label="See options"))
    elif result.reason == "hold_expired":
        text = "The hold ran out before the provider booked it, so nothing was booked."
        actions.append(
            NoticeAction(
                kind="refresh_hold", label="Get a fresh quote", approval_id=result.approval_id
            )
        )
    elif result.reason == "duplicate":
        text = "You already have this booking, so I didn't book it again. Nothing new was booked."
    elif result.reason == "provider_refused":
        text = "The provider refused that booking, so nothing was booked."
    elif result.reason == "not_booked":
        text = NOT_BOOKED_TEXT
        actions.append(
            NoticeAction(
                kind="refresh_hold", label="Get a fresh quote", approval_id=result.approval_id
            )
        )
    else:
        text = "That approval can't be used any more, so nothing was booked."
    return OutcomeNotice(outcome=RunOutcome.ACTION_FAILED, text=text, actions=actions)
