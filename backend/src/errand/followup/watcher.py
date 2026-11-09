"""The watcher (architecture 14.1, Gate 3 decisions 1 and 2).

A lifespan task polls `list_bookings` for every user with a conversation, mirrors every
upcoming booking the provider lists into `known_bookings` (source_intent_id NULL for ones
we did not make), and diffs. A booking that is `cancelled_by_provider` with a future slot
gets one transaction: the unique job row, the mirror update, and
`booking_no_longer_active` on every pending or granted cancel or reschedule card for it.
Then the tick delivers pending jobs and lapses the ones whose window has passed.

A quiet tick writes no trace. A tick with a change or a failure is a `watch` run: its own
trace with run.start and run.end, `kind: watch`, no outcome and no row in `runs` (INV-07).
A failed `list_bookings` skips that user until the next tick and shows as watcher lag in
`/api/health`. Evals and tests turn the interval off and call `tick()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from errand.clock import Clock, to_ms
from errand.domain.counters import RunCounters
from errand.followup import service
from errand.store.agent_db import AgentDB
from errand.store.repos.followups import job_id_for
from errand.store.repos.known_bookings import KnownBooking
from errand.tools.errors import ToolError
from errand.tools.gateway import Caller, ToolGateway, ToolLimits
from errand.tools.runtime import WorldAccess
from errand.trace.sinks.jsonl import JsonlSink, trace_path
from errand.trace.tracer import Tracer
from errand.world.contracts import ListBookingsOut

log = logging.getLogger(__name__)

WATCH_THREAD = "watch"


@dataclass
class Health:
    last_tick_at: int | None = None
    last_ok_at: int | None = None
    failing_users: list[str] = field(default_factory=list)

    def lag_s(self, now: datetime) -> float | None:
        if self.last_ok_at is None:
            return None
        return max(0.0, (to_ms(now) - self.last_ok_at) / 1000)


@dataclass
class TickResult:
    detected: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    lapsed: list[str] = field(default_factory=list)
    failed_users: list[str] = field(default_factory=list)
    trace: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(
            self.detected or self.delivered or self.ignored or self.lapsed or self.failed_users
        )


class Watcher:
    def __init__(
        self,
        *,
        db: AgentDB,
        world: WorldAccess,
        tz: tzinfo,
        traces_dir: Path,
        start: service.Starter,
        interval_s: float,
        extra_users: Callable[[], list[str]] = list,
    ) -> None:
        self.db = db
        self.world = world
        self.tz = tz
        self.traces_dir = traces_dir
        self.start = start
        self.interval_s = interval_s
        self.extra_users = extra_users
        self.health = Health()
        self._ticks = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def clock(self) -> Clock:
        return self.world.clock

    # ------------------------------------------------------------------ lifecycle

    def start_task(self) -> None:
        if self.interval_s > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="followup watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("watcher tick failed")
            await asyncio.sleep(self.interval_s)

    # ------------------------------------------------------------------ one tick

    async def tick(self, *, poll: bool = True) -> TickResult:
        """One tick. `poll=False` skips the provider reads and only delivers and lapses what
        the records already hold: that is the reconciler's pass (9.7), which resumes a job a
        crash left `detected` without touching the world."""
        async with self._lock:
            return await self._tick(poll)

    async def _tick(self, poll: bool) -> TickResult:
        self._ticks += 1
        now = self.clock.now()
        run_id = f"w_{to_ms(now)}_{self._ticks}"
        path = trace_path(self.traces_dir, WATCH_THREAD, run_id)
        tracer = Tracer(
            run_id=run_id, thread_id=WATCH_THREAD, sinks=[JsonlSink(path)], now=self.clock.now
        )
        result = TickResult(trace=path)
        tracer.start_run(trigger="watcher_tick", kind="watch")
        try:
            async with self.db.session() as repos:
                users = sorted({*await repos.conversations.user_ids(), *self.extra_users()})
            tools = ToolGateway(
                self.world.client,
                tracer=tracer,
                clock=self.clock,
                counters=RunCounters(),
                limits=ToolLimits(max_tool_calls=max(16, 4 * len(users) + 4)),
            )
            for user_id in users if poll else []:
                await self._watch_user(user_id, tools, tracer, result)
            delivery = await service.deliver_pending(
                self.db, start=self.start, now=self.clock.now, tz=self.tz, tracer=tracer
            )
            result.delivered, result.ignored = delivery.started, delivery.ignored
            result.lapsed = await service.lapse_due(
                self.db, start=self.start, now=self.clock.now, tz=self.tz, tracer=tracer
            )
        finally:
            self.health.last_tick_at = to_ms(self.clock.now())
            if poll:
                if not result.failed_users:
                    self.health.last_ok_at = self.health.last_tick_at
                self.health.failing_users = list(result.failed_users)
            changes: list[dict[str, Any]] = [{"detected": j} for j in result.detected]
            tracer.end_run(
                outcome=None,
                changes=changes,
                failed_users=result.failed_users,
            )
            tracer.close()
            if not result.changed:
                # A quiet tick leaves no trace at all.
                with contextlib.suppress(OSError):
                    path.unlink()
                    path.parent.rmdir()  # only when nothing else is in it
                result.trace = None
        return result

    async def _watch_user(
        self, user_id: str, tools: ToolGateway, tracer: Tracer, result: TickResult
    ) -> None:
        try:
            listed = await tools.call(Caller.WATCHER, "list_bookings", {"user_id": user_id})
        except ToolError:
            result.failed_users.append(user_id)
            return
        if not isinstance(listed, ListBookingsOut):
            result.failed_users.append(user_id)
            return
        now = self.clock.now()
        now_ms = to_ms(now)
        for b in listed.bookings:
            known = KnownBooking(
                booking_id=b.booking_id,
                user_id=user_id,
                provider_id=b.provider_id,
                service=b.service.value,
                slot_start=to_ms(b.slot.start),
                slot_end=to_ms(b.slot.end),
                status=b.status,
                last_seen_at=now_ms,
                origin_conversation_id=None,
                origin_brief_json=None,
                source_intent_id=None,
            )
            async with self.db.transaction() as repos:
                before = await repos.known_bookings.get(b.booking_id)
                # Only upcoming bookings are mirrored, and any already known keep tracking.
                if before is None and b.slot.end <= now:
                    continue
                await repos.known_bookings.mirror(known)
                # A move to cancelled_by_provider starts a job whatever the slot (the policy
                # ignores one that already started, S41); a booking first seen already
                # cancelled counts only with a future slot (it happened while we were down).
                moved = before is not None and before.status != "cancelled_by_provider"
                first_seen = before is None and b.slot.start > now
                if b.status != "cancelled_by_provider" or not (moved or first_seen):
                    continue
                created = await repos.followups.detect(
                    job_id=job_id_for(b.booking_id),
                    booking_id=b.booking_id,
                    event_kind="provider_cancel",
                    conversation_id=None,
                    now_ms=now_ms,
                )
                if not created:
                    continue
                invalidated = [
                    a.approval_id
                    for a in await repos.approvals.live_for_booking(
                        b.booking_id, "cancel", "reschedule"
                    )
                    if await repos.approvals.invalidate(a.approval_id, "booking_no_longer_active")
                ]
            result.detected.append(job_id_for(b.booking_id))
            tracer.emit(
                "followup.detected", booking_id=b.booking_id, job_id=job_id_for(b.booking_id)
            )
            for approval_id in invalidated:
                tracer.emit(
                    "approval.invalidated",
                    approval_id=approval_id,
                    reason="booking_no_longer_active",
                )
                tracer.emit(
                    "card.update",
                    approval_id=approval_id,
                    status="invalidated",
                    reason="booking_no_longer_active",
                )
