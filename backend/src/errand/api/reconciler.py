"""The reconciler: the one pass that settles what a restart or a stall left half done.

It reads our own tables, never LangGraph's `next` (9.7, the spike's L1 and L2), and it
changes nothing about the world itself. Everything it concludes comes from a run it
starts: a recovery run on the conversation's own thread, trigger `recovery`, whose
`ingress -> verify -> respond` applies the 9.4 rules unchanged (status first; a
confirmed booking is `BOOKED_VERIFIED`; a `not_found` on a live hold takes the same-key
re-send; past the hold's expiry it is definitive; an answer that cannot be had leaves
`PENDING_VERIFICATION`).

`startup()` runs in the API lifespan, before the app serves a request, and does
(architecture 9.9):

a. every unresolved intent with an attempt still `prepared`, `in_flight` or `unknown`
   gets a recovery run with `Recover(intent_id)`;
b. every `granted` approval no intent consumed is expired (`grant_not_executed`, its
   `pending_resumes` row closed) and gets a recovery run that says nothing was booked and
   shows a fresh card (`UiAction(refresh_hold)`). A grant is never resumed;
c. every run still `running` is dead (one API process per data directory). Each gets
   exactly one `run.end` (outcome `RUN_INTERRUPTED`, `recovered_by` naming the recovery
   run when there is one) appended to its own JSONL, and its RUN_INTERRUPTED message.

The process that wrote those runs is gone, so the pass takes over every lease it needs
(epoch + 1) instead of waiting out a TTL. A tick in a live process does not: it takes
the thread lease like any run, skips a busy thread, and tries again on the next tick.

`tick()` is pass (a) for `needs_attention` intents only. Three things call it: the
periodic task (`reconciler.interval_s`, 0 turns it off), the "Check now" action for one
conversation, and the dev drawer and the evals (`reconciler_tick`). A further pass, such as
N3's follow-up jobs, plugs in at `PASSES`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.agent import cards
from errand.agent.graph import run_config
from errand.agent.templates import run_interrupted
from errand.api.locks import thread_key, user_key
from errand.api.runs import RunManager
from errand.approvals import service
from errand.clock import to_ms, wall_now
from errand.config import ReconcilerSettings
from errand.domain.errand import Phase
from errand.domain.inbound import Inbound, Recover, UiAction
from errand.ids import message_id_for
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.messages import Message
from errand.store.repos.runs import Run
from errand.trace.reader import read_trace
from errand.trace.schema import RECORD_ADAPTER
from errand.trace.sinks.base import encode_record
from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.tracer import format_ts, trace_id_for

log = logging.getLogger(__name__)

# A further pass (N3's follow-up jobs) appends here: it gets the reconciler and returns
# the recovery runs it started.
Pass = Callable[["Reconciler", bool], Awaitable[list[str]]]
PASSES: list[Pass] = []


@dataclass
class Report:
    recovery_runs: list[str] = field(default_factory=list)
    closed_runs: list[str] = field(default_factory=list)
    expired_grants: list[str] = field(default_factory=list)
    skipped_busy: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(self, manager: RunManager, settings: ReconcilerSettings | None = None) -> None:
        self.manager = manager
        self.settings = settings or ReconcilerSettings()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ startup

    async def startup(self) -> Report:
        """The restart pass. Returns when every recovery run it started has ended."""
        async with self._lock:
            report = Report()
            db = self.manager.db
            async with db.session() as repos:
                dead = await repos.runs.list_running()
                intents = await repos.intents.recoverable()
            grants = await service.expire_unexecuted_grants(db)
            report.expired_grants = [g.approval_id for g in grants]
            dead_by_conversation: dict[str, list[Run]] = {}
            for run in dead:
                dead_by_conversation.setdefault(run.conversation_id, []).append(run)
            handled: set[str] = set()
            for intent in intents:
                if intent.conversation_id in handled:
                    continue
                handled.add(intent.conversation_id)
                run_id = await self._recover(
                    intent.conversation_id,
                    Recover(intent_id=intent.intent_id, after_restart=True),
                    dead_by_conversation.pop(intent.conversation_id, []),
                    report,
                    takeover=True,
                    user_id=intent.user_id,
                )
                if run_id:
                    report.recovery_runs.append(run_id)
            if handled and grants:
                # A conversation can hold both an unsettled intent and a grant nobody acted
                # on; the intent's run goes first, then the grant's fresh card.
                await self.manager.wait_idle()
            for grant in grants:
                run_id = await self._recover(
                    grant.conversation_id,
                    _fresh_card(grant),
                    dead_by_conversation.pop(grant.conversation_id, []),
                    report,
                    takeover=grant.conversation_id not in handled,
                )
                if run_id:
                    report.recovery_runs.append(run_id)
                await self.manager.wait_idle()
            for runs in dead_by_conversation.values():
                for run in runs:
                    await self.close_dead_run(
                        run, recovered_by=None, status_line=await self._status_line(run)
                    )
                    report.closed_runs.append(run.run_id)
            await self.manager.wait_idle()
            return report

    async def _recover(
        self,
        conversation_id: str,
        inbound: Inbound,
        dead: list[Run],
        report: Report,
        *,
        takeover: bool,
        user_id: str | None = None,
    ) -> str | None:
        """Close the conversation's dead runs, then start its recovery run."""
        manager = self.manager
        run_id = manager.ids.run_id()
        if takeover:
            async with manager.db.session() as repos:
                conversation = await repos.conversations.get(conversation_id)
                await repos.leases.take_over(thread_key(conversation_id))
                owner = user_id or (conversation.user_id if conversation else None)
                if owner is not None:
                    await repos.leases.take_over(user_key(owner))
        for run in dead:
            await self.close_dead_run(
                run, recovered_by=run_id, status_line=cards.STATUS_LINES[Phase.VERIFYING]
            )
            report.closed_runs.append(run.run_id)
        admission = await manager.start_recovery(conversation_id, inbound, run_id=run_id)
        if admission is None:
            report.skipped_busy.append(conversation_id)
            return None
        return admission.run_id

    # --------------------------------------------------------------------- tick

    async def tick(self, *, conversation_id: str | None = None, wait: bool = False) -> Report:
        """Re-check `needs_attention` intents in a running process (pass a only)."""
        report = Report()
        async with self._lock:
            async with self.manager.db.session() as repos:
                intents = [
                    i for i in await repos.intents.recoverable() if i.state == "needs_attention"
                ]
            seen: set[str] = set()
            for intent in intents:
                cid = intent.conversation_id
                if cid in seen or (conversation_id is not None and cid != conversation_id):
                    continue
                seen.add(cid)
                run_id = await self._recover(
                    cid,
                    Recover(intent_id=intent.intent_id, after_restart=False),
                    [],
                    report,
                    takeover=False,
                )
                if run_id:
                    report.recovery_runs.append(run_id)
            for extra in PASSES:
                report.recovery_runs.extend(await extra(self, False))
        if wait:
            await self.manager.wait_idle()
        return report

    # ----------------------------------------------------------------- periodic

    def start_periodic(self) -> None:
        if self.settings.interval_s <= 0 or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="reconciler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("reconciler tick failed; trying again next interval")

    # ------------------------------------------------------------- dead runs

    async def _status_line(self, run: Run) -> str | None:
        """The status line for a dead run's message when no recovery run follows: the
        phase of the errand as the last checkpoint holds it (10.8)."""
        try:
            snapshot = await self.manager.graph.aget_state(run_config(run.conversation_id))
            errand = (snapshot.values or {}).get("errand")
            phase = getattr(errand, "phase", None)
            return None if phase is None else cards.status_line(phase)
        except Exception:
            log.exception("could not read the checkpoint of %s", run.conversation_id)
            return None

    async def close_dead_run(
        self, run: Run, recovered_by: str | None, status_line: str | None = None
    ) -> None:
        """Give a run the process died in its one `run.end` and its message (decision 3).

        The Tracer's last write to the file may be torn (it writes each record in one
        `os.write`, but a kill can still cut it), so the append resumes after the last
        complete record and drops a torn tail. The runs row moves to `recovered`.
        """
        manager = self.manager
        path = manager.trace_file(run.conversation_id, run.run_id)
        now = manager.clock.now()
        _append_run_end(path, run, recovered_by, now)
        async with manager.db.transaction() as repos:
            if not await repos.messages.assistant_for_run(run.run_id):
                body = run_interrupted().model_copy(update={"status_line": status_line})
                await repos.messages.upsert_assistant(
                    Message(
                        message_id=message_id_for(run.run_id, "notice"),
                        conversation_id=run.conversation_id,
                        client_msg_id=None,
                        role="assistant",
                        kind="notice",
                        body=body.model_dump(mode="json"),
                        run_id=run.run_id,
                        turn_no=None,
                        approval_id=None,
                        typed_grant_ok=None,
                        rendered_at=None,
                        created_at=to_ms(now),
                    )
                )
            await repos.runs.mark_recovered(run.run_id, to_ms(now))


def _fresh_card(grant: ApprovalRow) -> UiAction:
    """The inbound of the recovery run for an approval that was granted and never acted on:
    the existing refresh path (a new hold, a new pending approval), flagged so that
    `respond` says what happened first."""
    return UiAction(
        client_action_id=f"recovery-{grant.approval_id}",
        kind="refresh_hold",
        payload={"approval_id": grant.approval_id, "recovery": "grant_not_executed"},
    )


def _append_run_end(path: Path, run: Run, recovered_by: str | None, now: datetime) -> None:
    """Append `run.end` to the run's JSONL unless it already has one (exactly one per run)."""
    wall = wall_now()
    records: list[dict[str, Any]] = []
    if path.exists():
        trace = read_trace(path)
        records = trace.records
        if trace.truncated_tail is not None:
            # A record cut off by the crash: drop the fragment, so the new line starts clean.
            with path.open("r+b") as f:
                f.truncate(path.stat().st_size - len(trace.truncated_tail))
    if any(r["type"] == "run.end" for r in records):
        return
    start = next((r for r in records if r["type"] == "run.start"), None)
    sink = JsonlSink(path)
    try:
        if start is None:
            # The process died before its first record. Give the run a start so the file is
            # a whole run: one run.start, one run.end.
            raw_start = {
                "seq": 1,
                "ts": format_ts(now),
                "wall_ts": format_ts(wall),
                "run_id": run.run_id,
                "thread_id": run.conversation_id,
                "trace_id": trace_id_for(run.run_id),
                "span_id": "s_1",
                "type": "run.start",
                "trigger": run.trigger,
            }
            record, line = encode_record(RECORD_ADAPTER.validate_python(raw_start))
            sink.write(record, line)
            start = record
            records = [record]
        totals = _totals(records, now)
        last = records[-1]
        raw_end = {
            "seq": int(last["seq"]) + 1,
            "ts": format_ts(now),
            "wall_ts": format_ts(wall),
            "run_id": run.run_id,
            "thread_id": run.conversation_id,
            "errand_id": next(
                (r["errand_id"] for r in reversed(records) if r.get("errand_id")), None
            ),
            "trace_id": trace_id_for(run.run_id),
            "span_id": start["span_id"],
            "type": "run.end",
            "outcome": "RUN_INTERRUPTED",
            "user_message_id": start.get("user_message_id"),
            "totals": totals,
            "recovered_by": recovered_by,
        }
        record, line = encode_record(RECORD_ADAPTER.validate_python(raw_end))
        sink.write(record, line)
    finally:
        sink.close()


def _totals(records: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    model_calls = tool_calls = reader_calls = tokens_in = tokens_out = 0
    cost = 0.0
    for r in records:
        kind = r["type"]
        usage = r.get("usage") or {}
        if kind in ("model.call", "reader.call"):
            tokens_in += sum(
                int(usage.get(k, 0))
                for k in ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h")
            )
            tokens_out += int(usage.get("output", 0))
            cost += float(r.get("cost_usd") or 0.0)
        if kind == "model.call":
            model_calls += 1
        elif kind == "reader.call" and not r.get("cache_hit") and r.get("usage") is not None:
            reader_calls += 1
        elif kind == "tool.call":
            tool_calls += 1
    return {
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "latency_ms": 0.0,
        "reader_calls": reader_calls,
    }
