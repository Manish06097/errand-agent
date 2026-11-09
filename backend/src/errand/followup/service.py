"""Job transitions and delivery (architecture 14, Gate 3 decisions 4 to 6, 15 and 16).

Delivery is a graph run, not a side channel: `deliver_pending` takes every job in `detected`
(M5's S22b rows included) and starts a run on that job's follow-up conversation with
`SystemEvent(followup_start, job_id)`. The tick calls it, and so will the reconciler (N1,
TODO). The follow-up conversation id is deterministic from the job id, so delivery is
idempotent; `notifications.event_id` (the job id) UNIQUE is the backstop.

`respond` writes the follow-up messages, the notification, the origin conversation's line
and the job move in one transaction through `commit_delivery`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from errand.clock import to_ms
from errand.domain.followup import FollowupCtx
from errand.domain.inbound import SystemEvent
from errand.domain.messages import ProactiveLine
from errand.followup import policy, search, templates
from errand.store.agent_db import AgentDB, Repos
from errand.store.repos.followups import Job, conversation_id_for
from errand.store.repos.known_bookings import KnownBooking
from errand.store.repos.messages import Message
from errand.store.repos.notifications import Notification
from errand.trace.tracer import Tracer

# Starts a run on a conversation (the RunManager's submit_system); raises if one is active.
Starter = Callable[[str, SystemEvent], Awaitable[None]]


def message_id_for(run_id: str, kind: str) -> str:
    return f"{run_id}-{kind}"


@dataclass(frozen=True)
class Gathered:
    job: Job
    known: KnownBooking
    facts: policy.Facts
    decision: policy.Decision
    reschedule_target: datetime | None = None


async def gather(
    repos: Repos, job: Job, *, now: datetime, tz: tzinfo, same_service: str | None = None
) -> Gathered | None:
    """Code gathers the facts the pure policy reads. Nothing here is persisted for them: a
    cancel or reschedule card that was pending or granted is read back from approvals
    invalidated with `booking_no_longer_active`, so a job resumed after a crash decides
    the same way. `same_service` is a confirmed booking of the same service inside the
    replacement window, from a fresh `list_bookings` (the caller's)."""
    known = await repos.known_bookings.get(job.booking_id)
    if known is None:
        return None
    from errand.clock import from_ms

    slot_start = from_ms(known.slot_start)
    closed = await repos.approvals.invalidated_for_booking(
        job.booking_id, "booking_no_longer_active"
    )
    cancel_card = any(a.action == "cancel" for a in closed)
    resched = [a for a in closed if a.action == "reschedule"]
    target = None
    if resched:
        to_start = resched[-1].material.get("to_start")
        target = datetime.fromisoformat(str(to_start)) if to_start else None
    facts = policy.Facts(
        slot_start=slot_start,
        now=now,
        replacement_succeeded=await repos.intents.replacement_succeeded(job.booking_id),
        same_service_in_window=same_service,
        cancel_card=cancel_card,
        reschedule_card=bool(resched),
    )
    return Gathered(job, known, facts, policy.decide(facts), target)


async def ignore_if_pointless(
    db: AgentDB, job: Job, *, now: datetime, tz: tzinfo, tracer: Tracer
) -> bool:
    """Rows 1 of the policy table (S41): the slot already started or a replacement already
    succeeded. The job ends `ignored` with no conversation, no message and no notification."""
    async with db.session() as repos:
        got = await gather(repos, job, now=now, tz=tz)
    if got is None or got.decision.action is not policy.Action.IGNORE:
        return False
    reason = got.decision.ignore_reason
    async with db.transaction() as repos:
        moved = await repos.followups.move(
            job.job_id, frm=("detected",), to="ignored", now_ms=to_ms(now)
        )
    if moved:
        tracer.emit(
            "followup.ignored",
            booking_id=job.booking_id,
            job_id=job.job_id,
            reason=str(reason.value if reason else ""),
        )
    return moved


@dataclass
class Delivery:
    started: list[str]
    ignored: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.started or self.ignored)


async def deliver_pending(
    db: AgentDB, *, start: Starter, now: Callable[[], datetime], tz: tzinfo, tracer: Tracer
) -> Delivery:
    """Start a follow-up run for every `detected` job. Returns what it started and ignored.

    A job whose conversation is busy (a run is active) is left `detected` for the next
    call; one that is no longer `detected` when re-read is skipped (another deliverer won).
    """
    delivery = Delivery([], [])
    started = delivery.started
    async with db.session() as repos:
        jobs = await repos.followups.in_state("detected")
    for job in jobs:
        if await ignore_if_pointless(db, job, now=now(), tz=tz, tracer=tracer):
            delivery.ignored.append(job.job_id)
            continue
        async with db.session() as repos:
            fresh = await repos.followups.get(job.job_id)
            known = await repos.known_bookings.get(job.booking_id)
        if fresh is None or fresh.state != "detected" or known is None:
            continue
        conversation_id = conversation_id_for(job.job_id)
        async with db.transaction() as repos:
            await repos.conversations.ensure_followup(
                conversation_id=conversation_id,
                user_id=known.user_id,
                origin_booking_id=job.booking_id,
                title=f"{templates.service_word(known.service).capitalize()} visit cancelled",
                now_ms=to_ms(now()),
            )
        try:
            await start(
                conversation_id,
                SystemEvent(kind="followup_start", job_id=job.job_id, booking_id=job.booking_id),
            )
        except Exception:
            continue
        started.append(job.job_id)
    return delivery


async def lapse_due(
    db: AgentDB, *, start: Starter, now: Callable[[], datetime], tz: tzinfo, tracer: Tracer
) -> list[str]:
    """Jobs whose replacement window has passed end `done(lapsed)` (decision 16). A notified
    job gets its line through a run (every message comes from a run); a job that was never
    delivered just ends."""
    from errand.clock import from_ms

    lapsed: list[str] = []
    async with db.session() as repos:
        jobs = await repos.followups.in_state("detected", "notified")
        known = {j.job_id: await repos.known_bookings.get(j.booking_id) for j in jobs}
    for job in jobs:
        k = known[job.job_id]
        if k is None or now() < search.window_end(from_ms(k.slot_start), tz):
            continue
        if job.state == "notified" and job.conversation_id is not None:
            try:
                await start(
                    job.conversation_id,
                    SystemEvent(
                        kind="followup_lapsed", job_id=job.job_id, booking_id=job.booking_id
                    ),
                )
            except Exception:
                continue
        else:
            async with db.transaction() as repos:
                await repos.followups.move(
                    job.job_id, frm=("detected",), to="done", now_ms=to_ms(now())
                )
            tracer.emit(
                "followup.done", booking_id=job.booking_id, job_id=job.job_id, reason="lapsed"
            )
        lapsed.append(job.job_id)
    return lapsed


# ---------------------------------------------------------------- written by respond


def bell_body(ctx: FollowupCtx, conversation_id: str, *, quiet: bool = False) -> dict[str, object]:
    return {
        "title": templates.bell_title(ctx.service, replacement_ready=ctx.ready),
        "conversation_id": conversation_id,
        "booking_id": ctx.booking_id,
        "ready": ctx.ready,
        "quiet": quiet,
    }


async def commit_delivery(
    repos: Repos,
    *,
    ctx: FollowupCtx,
    run_id: str,
    user_id: str,
    conversation_id: str,
    now_ms: int,
    tracer: Tracer,
) -> bool:
    """The job move, the bell row and the origin conversation's line, in respond's one
    transaction. Returns False when the job was no longer `detected` (nothing is written)."""
    moved = await repos.followups.move(
        ctx.job_id, frm=("detected",), to="notified", now_ms=now_ms, conversation_id=conversation_id
    )
    if not moved:
        return False
    await repos.notifications.insert(
        Notification(
            notification_id=f"n_{ctx.job_id}",
            user_id=user_id,
            conversation_id=conversation_id,
            kind="followup",
            urgency="urgent" if ctx.urgent else "normal",
            body=bell_body(ctx, conversation_id),
            event_id=ctx.job_id,
            created_at=now_ms,
            read_at=None,
        )
    )
    known = await repos.known_bookings.get(ctx.booking_id)
    if known is not None and known.origin_conversation_id:
        await _origin_line(
            repos,
            run_id=run_id,
            origin_conversation_id=known.origin_conversation_id,
            followup_conversation_id=conversation_id,
            booking_id=ctx.booking_id,
            text=templates.origin_line(ctx.provider_name, ctx.booking_id, ctx.slot_label),
            now_ms=now_ms,
        )
    tracer.emit(
        "followup.notified",
        booking_id=ctx.booking_id,
        job_id=ctx.job_id,
        urgency="urgent" if ctx.urgent else "normal",
        variant=ctx.variant,
    )
    return True


async def commit_done(
    repos: Repos, *, job_id: str, booking_id: str, reason: str, now_ms: int, tracer: Tracer
) -> bool:
    moved = await repos.followups.move(
        job_id, frm=("detected", "notified"), to="done", now_ms=now_ms
    )
    if moved:
        # Before migration 0005 (N2) the done reason lives in this record only.
        tracer.emit("followup.done", booking_id=booking_id, job_id=job_id, reason=reason)
    return moved


async def commit_replaced(
    repos: Repos,
    *,
    replaced_booking_id: str,
    new_booking_id: str,
    new_label: str,
    run_id: str,
    followup_conversation_id: str,
    now_ms: int,
    tracer: Tracer,
) -> None:
    """The rebooking is verified: the job is done(replaced) and the origin conversation
    hears about it."""
    from errand.store.repos.followups import job_id_for

    job_id = job_id_for(replaced_booking_id)
    await commit_done(
        repos,
        job_id=job_id,
        booking_id=replaced_booking_id,
        reason="replaced",
        now_ms=now_ms,
        tracer=tracer,
    )
    known = await repos.known_bookings.get(replaced_booking_id)
    if known is not None and known.origin_conversation_id:
        await _origin_line(
            repos,
            run_id=run_id,
            origin_conversation_id=known.origin_conversation_id,
            followup_conversation_id=followup_conversation_id,
            booking_id=replaced_booking_id,
            text=templates.origin_line_rebooked(new_booking_id, new_label, replaced_booking_id),
            now_ms=now_ms,
        )


async def _origin_line(
    repos: Repos,
    *,
    run_id: str,
    origin_conversation_id: str,
    followup_conversation_id: str,
    booking_id: str,
    text: str,
    now_ms: int,
) -> None:
    body = ProactiveLine(
        text=text, followup_conversation_id=followup_conversation_id, booking_id=booking_id
    )
    await repos.messages.upsert_assistant(
        Message(
            message_id=message_id_for(run_id, "proactive"),
            conversation_id=origin_conversation_id,
            client_msg_id=None,
            role="assistant",
            kind="proactive",
            body=json.loads(body.model_dump_json()),
            run_id=run_id,
            turn_no=None,
            approval_id=None,
            typed_grant_ok=None,
            rendered_at=None,
            created_at=now_ms,
        )
    )
    await repos.conversations.touch(origin_conversation_id, now_ms)
