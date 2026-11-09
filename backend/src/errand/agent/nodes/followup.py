"""followup: the proactive follow-up as a graph node (architecture 14, Gate 3).

Entered from `ingress` by `SystemEvent(followup_start | followup_lapsed)` and by the
`widen_followup` and `dismiss_followup` actions. No model is called, ever: the node runs
the pure policy over facts it gathers, builds the replacement brief, searches with the
normal filters and code ranking, and hands a proposal to `prepare_proposal` (the Rebook
card, through the same hold, fingerprint and fresh approval as any booking) or a notice to
`respond`. Nothing here books; the only way to `book` is a grant on the new card.

This module imports neither `errand.llm` nor the reader (an import-linter contract). The
run's dependencies are taken as the structural `FollowupDeps`, which `RunDeps` satisfies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Protocol

from langgraph.runtime import Runtime

from errand.agent.state import ThreadState
from errand.agent.transitions import transition
from errand.approvals import service as approvals
from errand.clock import from_ms
from errand.domain import timewin
from errand.domain.brief import ErrandKind
from errand.domain.errand import Errand, Phase, RebookInfo
from errand.domain.followup import AltSlot, FollowupCtx, WidenOption
from errand.domain.inbound import SystemEvent, UiAction
from errand.domain.outcomes import FollowupDone, Proposal
from errand.domain.windows import slot_label
from errand.followup import brief as fbrief
from errand.followup import policy, search, service, templates
from errand.ids import errand_id_for
from errand.store.agent_db import AgentDB
from errand.store.repos.followups import Job, job_id_for
from errand.store.repos.known_bookings import KnownBooking
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller, ToolGateway
from errand.trace.tracer import Tracer
from errand.world.contracts import (
    BookingItem,
    ListBookingsOut,
    SearchProvidersOut,
)

TERMINAL = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.ABANDONED})


class FollowupDeps(Protocol):
    run_id: str
    conversation_id: str
    user_id: str
    tracer: Tracer
    tools: ToolGateway
    db: AgentDB
    tz: tzinfo

    @property
    def clock(self) -> Any: ...


@dataclass
class _Out:
    ctx: FollowupCtx
    errand: Errand | None = None
    proposal: Proposal | None = None


async def followup(state: ThreadState, runtime: Runtime[Any]) -> dict[str, Any]:
    deps: FollowupDeps = runtime.context
    phase = state.errand.phase.value if state.errand else None
    with deps.tracer.node("followup", phase_before=phase) as span:
        out = await _Followup(deps, state).run()
        span.end_fields["phase_after"] = out.errand.phase.value if out.errand else phase
    update: dict[str, Any] = {"followup": out.ctx}
    if out.errand is not None:
        update["errand"] = out.errand
    if out.proposal is not None:
        update["plan_result"] = out.proposal
        update["result"] = None
    else:
        update["plan_result"] = None
        update["result"] = FollowupDone()
    return update


def route_after_followup(state: ThreadState) -> str:
    return "prepare_proposal" if state.plan_result is not None else "respond"


class _Followup:
    def __init__(self, deps: FollowupDeps, state: ThreadState) -> None:
        self.deps = deps
        self.state = state
        self.now: datetime = deps.clock.now()

    async def run(self) -> _Out:
        inbound = self.state.inbound
        if isinstance(inbound, SystemEvent) and inbound.kind == "followup_start":
            return await self._start(inbound)
        if isinstance(inbound, SystemEvent) and inbound.kind == "followup_lapsed":
            return await self._lapse(inbound)
        if isinstance(inbound, UiAction) and inbound.kind == "widen_followup":
            return await self._widen(inbound)
        if isinstance(inbound, UiAction) and inbound.kind == "dismiss_followup":
            return await self._dismiss()
        return _Out(FollowupCtx(job_id="", booking_id="", variant="stale"))

    # ------------------------------------------------------------------ helpers

    async def _job(self, job_id: str | None) -> tuple[Job, KnownBooking] | None:
        if not job_id:
            return None
        async with self.deps.db.session() as repos:
            job = await repos.followups.get(job_id)
            known = await repos.known_bookings.get(job.booking_id) if job else None
        return (job, known) if job and known else None

    async def _job_of_conversation(self) -> tuple[Job, KnownBooking] | None:
        async with self.deps.db.session() as repos:
            conv = await repos.conversations.get(self.deps.conversation_id)
        if conv is None or conv.origin_booking_id is None:
            return None
        return await self._job(job_id_for(conv.origin_booking_id))

    async def _bookings(self) -> list[BookingItem] | None:
        try:
            listed = await self.deps.tools.call(
                Caller.FOLLOWUP, "list_bookings", {"user_id": self.deps.user_id}
            )
        except ToolError:
            return None
        return list(listed.bookings) if isinstance(listed, ListBookingsOut) else None

    def _label(self, known: KnownBooking, *, urgent: bool) -> str:
        return templates.when(
            from_ms(known.slot_start), self.deps.tz, now=self.now if urgent else None
        )

    async def _approved_price(self, known: KnownBooking) -> int | None:
        if known.source_intent_id is None:
            return None
        async with self.deps.db.session() as repos:
            intent = await repos.intents.get(known.source_intent_id)
            approval = await repos.approvals.get(intent.approval_id) if intent else None
        price = approval.card.get("quoted_price_inr") if approval else None
        return int(price) if isinstance(price, int) else None

    # ------------------------------------------------------------------ start

    async def _start(self, inbound: SystemEvent) -> _Out:
        got = await self._job(inbound.job_id)
        if got is None or got[0].state != "detected":
            return _Out(FollowupCtx(job_id=inbound.job_id or "", booking_id="", variant="stale"))
        job, known = got
        deps = self.deps
        bookings = await self._bookings()
        names = {b.booking_id: b for b in bookings or []}
        provider_name = (
            names[job.booking_id].provider_name if job.booking_id in names else known.provider_id
        )
        slot_start = from_ms(known.slot_start)
        # Another confirmed booking of the same service inside the replacement window.
        interval = search.replacement_window(slot_start, self.now, deps.tz)
        same = None
        for b in bookings or []:
            if (
                b.booking_id != job.booking_id
                and b.status == "confirmed"
                and b.service.value == known.service
                and interval is not None
                and interval.start <= b.slot.start < interval.end
            ):
                same = b
                break
        async with deps.db.session() as repos:
            got_facts = await service.gather(
                repos, job, now=self.now, tz=deps.tz, same_service=same.booking_id if same else None
            )
        assert got_facts is not None
        decision = got_facts.decision
        urgent = decision.urgency is policy.Urgency.URGENT
        ctx = FollowupCtx(
            job_id=job.job_id,
            booking_id=job.booking_id,
            urgent=urgent,
            provider_name=provider_name,
            service=known.service,
            slot_label=self._label(known, urgent=urgent),
        )
        if decision.action is policy.Action.NOTIFY_NO_SEARCH and same is not None:
            existing = (
                same.provider_name,
                templates.when(same.slot.start, deps.tz),
                same.booking_id,
            )
            return _Out(ctx.model_copy(update={"variant": "no_search", "existing": existing}))
        if decision.action is policy.Action.NOTIFY_NOTHING_TO_CANCEL:
            return _Out(ctx.model_copy(update={"variant": "nothing_to_cancel"}))
        if not decision.searches:
            return _Out(ctx.model_copy(update={"variant": "stale"}))
        interval = search.replacement_window(
            slot_start,
            self.now,
            deps.tz,
            urgent=urgent,
            reschedule_target=got_facts.reschedule_target
            if decision.use_reschedule_window
            else None,
        )
        if interval is None:
            return _Out(ctx.model_copy(update={"variant": "nothing_fits", "ready": False}))
        brief = fbrief.replacement_brief(
            known, interval=interval, tz=deps.tz, approved_price=await self._approved_price(known)
        )
        errand = Errand(
            errand_id=errand_id_for(deps.run_id),
            kind=ErrandKind.REBOOK,
            phase=Phase.GATHERING,
            origin="provider_cancel",
            brief=brief,
            replaces_booking_id=job.booking_id,
            rebook=RebookInfo(
                booking_id=job.booking_id,
                provider_name=provider_name,
                slot_label=templates.when(slot_start, deps.tz),
                price_inr=await self._approved_price(known),
            ),
        )
        deps.tracer.errand_id = errand.errand_id
        if brief.area is None:
            return _Out(ctx.model_copy(update={"variant": "ask_area"}), errand)
        return await self._search(errand, ctx, known.provider_id)

    # ------------------------------------------------------------------ search

    async def _search(self, errand: Errand, ctx: FollowupCtx, cancelling_provider: str) -> _Out:
        deps = self.deps
        brief = errand.brief
        assert brief.service and brief.area and brief.window
        window = brief.window.value
        ceiling = (
            brief.budget_max_inr.value
            if brief.budget_max_inr and isinstance(brief.budget_max_inr.value, int)
            else None
        )
        answer = await deps.tools.call(
            Caller.FOLLOWUP,
            "search_providers",
            {
                "service": brief.service.value,
                "area": brief.area.value,
                "date_range": timewin.date_range(window, deps.tz),
            },
        )
        if not isinstance(answer, SearchProvidersOut):
            ctx = ctx.model_copy(update={"variant": "nothing_fits", "ready": False})
            return _Out(ctx, errand)
        avoid = (
            frozenset(brief.provider_pref.value.avoid_ids) if brief.provider_pref else frozenset()
        )
        built = search.build_shortlist(
            answer,
            window=window,
            ceiling=ceiling,
            avoid=avoid,
            cancelling_provider=cancelling_provider,
            brief_version=brief.version,
            tz=deps.tz,
        )
        searched = window.label + (f", up to ₹{ceiling:,}" if ceiling is not None else "")
        shortlist = built.shortlist
        errand = errand.model_copy(update={"shortlist": shortlist})
        if not shortlist.candidates:
            widen = [
                WidenOption(field="days", label="More days (the next 7 days)"),
                *(
                    [
                        WidenOption(
                            field="price",
                            label=f"Up to ₹{built.cheapest_over_ceiling:,}",
                            value=str(built.cheapest_over_ceiling),
                        )
                    ]
                    if built.cheapest_over_ceiling is not None
                    else []
                ),
                WidenOption(field="time", label="Any time of day"),
            ]
            ctx = ctx.model_copy(
                update={
                    "variant": "nothing_fits",
                    "searched": searched,
                    "widen": widen,
                    "ready": False,
                }
            )
            return _Out(ctx, errand)
        top = shortlist.candidates[0]
        first = top.slots[0]
        errand = _to_comparing(errand, deps.tracer)
        alternatives = _alternatives(shortlist, deps.tz)
        ctx = ctx.model_copy(
            update={
                "variant": "replacement",
                "searched": searched,
                "alternatives": alternatives,
                "ready": True,
            }
        )
        return _Out(
            ctx,
            errand,
            Proposal(action="book", provider_id=top.provider_id, slot_ref=first.ref),
        )

    # ------------------------------------------------------------------ widen, dismiss, lapse

    async def _widen(self, inbound: UiAction) -> _Out:
        errand = self.state.errand
        got = await self._job_of_conversation()
        if errand is None or errand.kind is not ErrandKind.REBOOK or got is None:
            return _Out(FollowupCtx(job_id="", booking_id="", mode="widen", variant="stale"))
        job, known = got
        deps = self.deps
        brief = fbrief.widened(
            errand.brief,
            inbound.payload.get("field", ""),
            inbound.payload.get("value"),
            tz=deps.tz,
            booking_id=job.booking_id,
        )
        errand = errand.model_copy(update={"brief": brief, "shortlist": None})
        if errand.phase is not Phase.GATHERING:
            errand = (
                transition(errand, Phase.GATHERING, "followup_widened", deps.tracer)
                if Phase.GATHERING in _ALLOWED(errand)
                else errand
            )
        provider = errand.rebook.provider_name if errand.rebook else ""
        ctx = FollowupCtx(
            job_id=job.job_id,
            booking_id=job.booking_id,
            mode="widen",
            provider_name=provider,
            service=known.service,
            slot_label=errand.rebook.slot_label if errand.rebook else "",
        )
        return await self._search(errand, ctx, known.provider_id)

    async def _dismiss(self) -> _Out:
        got = await self._job_of_conversation()
        errand = self.state.errand
        if got is None:
            return _Out(FollowupCtx(job_id="", booking_id="", mode="dismiss", variant="stale"))
        job, _ = got
        await self._invalidate_live("followup_dismissed")
        ctx = FollowupCtx(
            job_id=job.job_id, booking_id=job.booking_id, mode="dismiss", variant="dismissed"
        )
        return _Out(ctx, _abandon(errand, "followup_dismissed", self.deps.tracer))

    async def _lapse(self, inbound: SystemEvent) -> _Out:
        got = await self._job(inbound.job_id)
        if got is None or got[0].state not in ("detected", "notified"):
            return _Out(
                FollowupCtx(
                    job_id=inbound.job_id or "", booking_id="", mode="lapse", variant="stale"
                )
            )
        job, _ = got
        await self._invalidate_live("followup_expired")
        ctx = FollowupCtx(
            job_id=job.job_id, booking_id=job.booking_id, mode="lapse", variant="lapsed"
        )
        return _Out(ctx, _abandon(self.state.errand, "followup_expired", self.deps.tracer))

    async def _invalidate_live(self, reason: str) -> None:
        deps = self.deps
        async with deps.db.session() as repos:
            live = await repos.approvals.live_for_conversation(deps.conversation_id)
        if live is not None and await approvals.invalidate(deps.db, live.approval_id, reason):
            deps.tracer.emit("approval.invalidated", approval_id=live.approval_id, reason=reason)
            deps.tracer.emit(
                "card.update", approval_id=live.approval_id, status="invalidated", reason=reason
            )


def _ALLOWED(errand: Errand) -> frozenset[Phase]:
    from errand.agent.transitions import ALLOWED

    return ALLOWED[errand.phase]


def _to_comparing(errand: Errand, tracer: Tracer) -> Errand:
    if errand.phase is Phase.GATHERING:
        return transition(errand, Phase.COMPARING, "shortlist_built", tracer)
    return errand


def _abandon(errand: Errand | None, reason: str, tracer: Tracer) -> Errand | None:
    if errand is None or errand.phase in TERMINAL:
        return errand
    if Phase.ABANDONED in _ALLOWED(errand):
        return transition(errand, Phase.ABANDONED, reason, tracer)
    return errand


def _alternatives(shortlist: Any, tz: tzinfo) -> list[AltSlot]:
    """The next two candidates as slot buttons (decision 12): other providers' first slots,
    then the top provider's own further slots, never more than two."""
    top = shortlist.candidates[0]
    picks: list[tuple[Any, Any]] = [(c, c.slots[0]) for c in shortlist.candidates[1:3]]
    for extra in top.slots[1:]:
        if len(picks) >= 2:
            break
        picks.append((top, extra))
    return [
        AltSlot(
            slot_ref=h.ref,
            label=f"{slot_label(h.slot, tz)} with {c.name}, ₹{c.price_inr:,}",
        )
        for c, h in picks[:2]
    ]
