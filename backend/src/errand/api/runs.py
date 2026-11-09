"""RunManager: admits inputs and runs each graph invocation as a detached task.

Admission, under the thread's lock and nothing else (6.6):
1. `client_msg_id` / `client_action_id` dedupe: a retried POST gets the run it already
   started. This comes before the active-run check, or a client retrying its own
   message mid-run would get a 409 for it.
2. one active run per thread: anything else gets 409 `run_in_progress`;
3. the thread lease (`thread:<id>`), renewed for the whole run; the run id is minted
   only after it, so a refused lease never burns an id;
4. for a message: the typed-grant decision (approvals/typed_grant.py) after the sweep,
   and the grant itself when it holds; for an approval decision: the grant or decline
   (approvals/service.py), with the `pending_resumes` row in the grant's transaction;
5. the interrupt check, which decides plain input or `Command(resume=...)`: any input
   to a parked thread is a resume (spike L5);
6. the user message (if any), the runs row and the task, then the lock is released.

A run is a task the manager owns, with a strong reference held until it finishes, so it
is never garbage-collected and never tied to the request that started it: a closed tab
or a dropped SSE connection cannot cancel it (ADR 0011). Each run gets its own tracer
(JSONL first, then the live stream) and its own model and tool gateways, which share
one `RunCounters` with the run's quarantined review reader.

Every run ends with exactly one `run.end` and a user-visible message. `respond` writes
the normal one; if anything raises (or the 90 s backstop fires), the outer handler
here writes the INTERNAL_ERROR template itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.types import Command

from errand.agent import critical, memory_runend
from errand.agent.deps import ConsequentialLocks, RunDeps
from errand.agent.graph import Graph, RoutedAs, invoke, route_input, run_config
from errand.agent.planner_tools import PLANNER_TOOLS
from errand.agent.templates import backstop_notice, internal_error
from errand.api.locks import LeaseKeeper, ThreadLocks, UserLocks, thread_key
from errand.api.sse import Broadcaster, RunStream
from errand.approvals import service
from errand.approvals.typed_grant import Decision, VisibleCard, decide
from errand.clock import Clock, to_ms
from errand.config import (
    ApprovalSettings,
    BookingSettings,
    LimitSettings,
    ModelSettings,
    ReaderSettings,
)
from errand.domain.counters import RunCounters
from errand.domain.errand import Errand
from errand.domain.inbound import (
    ApprovalDeclined,
    ApprovalGranted,
    Inbound,
    SystemEvent,
    UiAction,
    UserMessage,
)
from errand.domain.messages import OutcomeNotice
from errand.domain.outcomes import Limit, PendingCheck, RunOutcome
from errand.ids import IdFactory, message_id_for, message_id_for_user
from errand.llm.gateway import LLMGateway
from errand.memory.service import MemoryService
from errand.quarantine.reader import ReviewReader
from errand.store.agent_db import AgentDB
from errand.store.repos.messages import Message
from errand.store.repos.runs import RunTotals
from errand.tools.gateway import ToolGateway, ToolLimits
from errand.tools.runtime import WorldAccess
from errand.trace.sinks.base import ProcessSink, Sink
from errand.trace.sinks.jsonl import JsonlSink, trace_path
from errand.trace.sinks.null import NullSink
from errand.trace.sinks.stream import StreamSink
from errand.trace.tracer import Tracer

log = logging.getLogger(__name__)

# Makes a scripted planner for one run (None in live mode: the gateway builds ChatAnthropic).
ModelFactory = Callable[[], BaseChatModel | None]
RECOVERY_TRIGGER = "recovery"
TRIGGERS: dict[RoutedAs, str] = {
    "input": "user_message",
    "resume": "user_message_resume",
    "grant": "typed_grant",
}


def _no_reader() -> None:
    return None


class RunInProgress(Exception):
    def __init__(self, run_id: str | None) -> None:
        super().__init__("a run is already active on this conversation")
        self.run_id = run_id


class UnknownConversation(LookupError):
    pass


class ClientMsgIdReused(Exception):
    """The same client_msg_id was already used in another conversation."""


class ShuttingDown(Exception):
    pass


class UnknownApproval(LookupError):
    pass


@dataclass(frozen=True)
class Admission:
    run_id: str
    routed_as: RoutedAs
    deduped: bool


@dataclass(frozen=True)
class Answer:
    """An HTTP answer for a decision or action: status code and JSON body."""

    status: int
    body: dict[str, Any]


@dataclass
class _Run:
    run_id: str
    conversation_id: str
    user_id: str
    inbound: Inbound
    counters: RunCounters
    graph_input: dict[str, Any] | Command[Any]
    routed_as: RoutedAs
    trigger: str
    lease: LeaseKeeper
    stream: RunStream
    deadline_s: float = 0.0
    user_message_id: str | None = None
    client: dict[str, Any] | None = None
    deps: RunDeps | None = None


class RunManager:
    def __init__(
        self,
        *,
        graph: Graph,
        db: AgentDB,
        world: WorldAccess,
        ids: IdFactory,
        traces_dir: Path,
        tz: tzinfo,
        model_settings: ModelSettings,
        limits: LimitSettings,
        model_factory: ModelFactory,
        broadcaster: Broadcaster | None = None,
        approval_settings: ApprovalSettings | None = None,
        booking_settings: BookingSettings | None = None,
        consequential: ConsequentialLocks | None = None,
        reader_factory: ModelFactory | None = None,
        reader_settings: ReaderSettings | None = None,
        trace_sink: ProcessSink | None = None,
    ) -> None:
        self.graph = graph
        self.db = db
        self.world = world
        self.ids = ids
        self.traces_dir = traces_dir
        self.tz = tz
        self.model_settings = model_settings
        self.limits = limits
        self.model_factory = model_factory
        # The scripted review reader for one run (None in live mode, like the planner).
        self.reader_factory = reader_factory or _no_reader
        self.reader_settings = reader_settings or ReaderSettings()
        self.broadcaster = broadcaster or Broadcaster()
        # The optional dashboard sink (Langfuse or NullSink), owned by the lifespan.
        self.trace_sink: ProcessSink = trace_sink or NullSink()
        self._locks = ThreadLocks()
        self._active: dict[str, str] = {}
        # Strong references: a task nobody holds can be garbage-collected mid-run.
        self._tasks: set[asyncio.Task[None]] = set()
        # Unique per live owner: a restarted process often gets the same pid back (always,
        # in a container), and its admission counter starts at 1 again (BUG-M3-4). The
        # holder stays in the leases table, so the random part never reaches a trace.
        self._holder = f"api-{os.getpid()}-{secrets.token_hex(8)}"
        self._admissions = 0
        self._closing = False
        self.approval_settings = approval_settings or ApprovalSettings()
        self.booking_settings = booking_settings or BookingSettings()
        self.consequential: ConsequentialLocks = consequential or UserLocks(db, self._holder)
        # Set by the app once the memory service exists (it needs the manager's clock).
        self.memory: MemoryService | None = None
        # The follow-up watcher, set by the lifespan; the reconciler's follow-up pass reads it.
        self.followup_watcher: Any = None

    @property
    def approval_policy(self) -> service.ApprovalPolicy:
        s = self.approval_settings
        return service.ApprovalPolicy(
            ttl_ms=round(s.ttl_s * 1000), grace_ms=round(s.execute_grace_s * 1000)
        )

    @property
    def clock(self) -> Clock:
        return self.world.clock

    def active_run(self, conversation_id: str) -> str | None:
        return self._active.get(conversation_id)

    # ------------------------------------------------------------------ admission

    async def _take_lease(self, conversation_id: str) -> LeaseKeeper:
        if self._active.get(conversation_id) is not None:
            raise RunInProgress(self._active[conversation_id])
        # The holder names this admission, not the run: the run id is minted only
        # once the lease is ours, so a refused lease never burns one (BUG-M3-2).
        self._admissions += 1
        lease = await LeaseKeeper.acquire(
            self.db, thread_key(conversation_id), f"{self._holder}:a{self._admissions}"
        )
        if lease is None:
            raise RunInProgress(None)
        return lease

    async def submit_message(
        self,
        conversation_id: str,
        *,
        text: str,
        client_msg_id: str,
        client_composed_at: str | None = None,
        visible_card: VisibleCard | None = None,
        composed_at_ms: int | None = None,
    ) -> Admission:
        if self._closing:
            raise ShuttingDown()
        async with self._locks.get(conversation_id):
            async with self.db.session() as repos:
                conversation = await repos.conversations.get(conversation_id)
                if conversation is None:
                    raise UnknownConversation(conversation_id)
                existing = await repos.messages.get_by_client_msg_id(client_msg_id)
                if existing is not None:
                    return await self._dedupe(existing, conversation_id, repos.runs)
                turn_no = await repos.messages.count_user_messages(conversation_id) + 1
            lease = await self._take_lease(conversation_id)
            run_id = self.ids.run_id()
            try:
                message_id = message_id_for_user(run_id)
                inbound, routed = await self._typed(
                    conversation_id,
                    UserMessage(
                        message_id=message_id,
                        client_msg_id=client_msg_id,
                        text=text,
                        turn_no=turn_no,
                    ),
                    visible_card,
                    composed_at_ms,
                )
                client = {
                    "client_composed_at": client_composed_at,
                    "visible_card": None
                    if visible_card is None
                    else {
                        "approval_id": visible_card.approval_id,
                        "fingerprint": visible_card.fingerprint,
                        "message_id": visible_card.message_id,
                    },
                }
                return await self._admit(
                    run_id,
                    conversation_id,
                    conversation.user_id,
                    inbound,
                    lease,
                    routed=routed,
                    user_text=text,
                    client_msg_id=client_msg_id,
                    turn_no=turn_no,
                    client=client,
                )
            except BaseException:
                await lease.release()
                raise

    async def _typed(
        self,
        conversation_id: str,
        message: UserMessage,
        visible_card: VisibleCard | None,
        composed_at_ms: int | None,
    ) -> tuple[Inbound, RoutedAs | None]:
        """The typed-grant decision (8.3), under the thread lock and after the sweep."""
        now_ms = to_ms(self.clock.now())
        live, _ = await service.live_approval(
            self.db, conversation_id, now_ms, self.approval_policy
        )
        if live is None:
            return message, None
        async with self.db.session() as repos:
            latest = await repos.messages.latest_assistant(conversation_id)
        verdict = decide(
            text=message.text,
            live=live,
            visible_card=visible_card,
            composed_at_ms=composed_at_ms,
            latest=latest,
            now_ms=now_ms,
            tolerance_ms=round(self.approval_settings.composed_tolerance_s * 1000),
        )
        if verdict.decision is Decision.HINT:
            return message.model_copy(update={"confirm_hint": True}), None
        if verdict.decision is not Decision.GRANT:
            return message, None
        granted = ApprovalGranted(
            approval_id=live.approval_id, via="typed", message_id=message.message_id
        )
        result = await service.grant(
            self.db,
            approval_id=live.approval_id,
            fingerprint=live.fingerprint,
            via="typed",
            message_id=message.message_id,
            resume_inbound=granted.model_dump(mode="json"),
            now_ms=now_ms,
            policy=self.approval_policy,
        )
        if result.outcome is service.GrantOutcome.GRANTED:
            return granted, "grant"
        return message, None

    async def submit_decision(
        self,
        approval_id: str,
        *,
        decision: str,
        fingerprint: str,
        client_action_id: str,
    ) -> Answer:
        """A button press on a card: the only button grant path (8.3)."""
        if self._closing:
            raise ShuttingDown()
        async with self.db.session() as repos:
            row = await repos.approvals.get(approval_id)
        if row is None:
            raise UnknownApproval(approval_id)
        conversation_id = row.conversation_id
        async with self._locks.get(conversation_id):
            stored = await self._stored(client_action_id, "decision")
            if stored is not None:
                return stored
            lease = await self._take_lease(conversation_id)
            try:
                answer = await self._decide(
                    row.conversation_id, approval_id, decision, fingerprint, lease
                )
            except BaseException:
                await lease.release()
                raise
            if not answer.body.get("_started"):
                await lease.release()
            answer.body.pop("_started", None)
            await self._remember(client_action_id, "decision", answer)
            return answer

    async def _decide(
        self,
        conversation_id: str,
        approval_id: str,
        decision: str,
        fingerprint: str,
        lease: LeaseKeeper,
    ) -> Answer:
        now_ms = to_ms(self.clock.now())
        async with self.db.session() as repos:
            conversation = await repos.conversations.get(conversation_id)
        assert conversation is not None
        if decision == "decline":
            result = await service.decline(
                self.db, approval_id=approval_id, now_ms=now_ms, policy=self.approval_policy
            )
            if result.outcome is service.GrantOutcome.GRANTED:
                admission = await self._admit(
                    self.ids.run_id(),
                    conversation_id,
                    conversation.user_id,
                    ApprovalDeclined(approval_id=approval_id),
                    lease,
                    trigger="approval_decision",
                )
                return Answer(
                    202, {"status": "declined", "run_id": admission.run_id, "_started": True}
                )
            return _refusal(result)
        granted = ApprovalGranted(approval_id=approval_id, via="button")
        result = await service.grant(
            self.db,
            approval_id=approval_id,
            fingerprint=fingerprint,
            via="button",
            message_id=None,
            resume_inbound=granted.model_dump(mode="json"),
            now_ms=now_ms,
            policy=self.approval_policy,
        )
        if result.outcome is service.GrantOutcome.GRANTED:
            admission = await self._admit(
                self.ids.run_id(),
                conversation_id,
                conversation.user_id,
                granted,
                lease,
                trigger="approval_decision",
            )
            async with self.db.session() as repos:
                await repos.approvals.mark_resume_started(approval_id, admission.run_id)
            return Answer(202, {"status": "granted", "run_id": admission.run_id, "_started": True})
        if result.outcome is service.GrantOutcome.ALREADY and result.row is not None:
            body: dict[str, Any] = {"status": "already_approved", "detail": result.row.status}
            if result.row.status == "granted":
                # Granted but nothing is running it (no active run: we hold the lease):
                # the click starts the resume from its pending_resumes row (9.7).
                async with self.db.session() as repos:
                    pending = await repos.approvals.get_resume(approval_id)
                if pending is not None:
                    inbound = ApprovalGranted.model_validate_json(pending.inbound_json)
                    admission = await self._admit(
                        self.ids.run_id(),
                        conversation_id,
                        conversation.user_id,
                        inbound,
                        lease,
                        trigger="approval_resume",
                    )
                    async with self.db.session() as repos:
                        await repos.approvals.mark_resume_started(approval_id, admission.run_id)
                    body |= {"run_id": admission.run_id, "_started": True}
            return Answer(200, body)
        return _refusal(result)

    async def submit_action(
        self,
        conversation_id: str,
        *,
        client_action_id: str,
        kind: str,
        payload: dict[str, str],
    ) -> Answer:
        if self._closing:
            raise ShuttingDown()
        async with self._locks.get(conversation_id):
            async with self.db.session() as repos:
                conversation = await repos.conversations.get(conversation_id)
            if conversation is None:
                raise UnknownConversation(conversation_id)
            stored = await self._stored(client_action_id, "actions")
            if stored is not None:
                return stored
            if kind == "check_now":
                nothing = await self._nothing_to_check(conversation_id)
                if nothing is not None:
                    await self._remember(client_action_id, "actions", nothing)
                    return nothing
            lease = await self._take_lease(conversation_id)
            try:
                inbound = UiAction(
                    client_action_id=client_action_id,
                    kind=kind,  # type: ignore[arg-type]
                    payload=payload,
                )
                admission = await self._admit(
                    self.ids.run_id(),
                    conversation_id,
                    conversation.user_id,
                    inbound,
                    lease,
                    trigger="ui_action",
                )
            except BaseException:
                await lease.release()
                raise
            answer = Answer(202, {"run_id": admission.run_id, "routed_as": admission.routed_as})
            await self._remember(client_action_id, "actions", answer)
            return answer

    async def submit_system(self, conversation_id: str, event: SystemEvent) -> Admission:
        """Start a run the system asked for (a follow-up delivery or lapse, 14). Same
        admission as any input: the thread lock and lease, so a busy conversation raises
        `RunInProgress` and the caller tries again on its next tick."""
        if self._closing:
            raise ShuttingDown()
        async with self._locks.get(conversation_id):
            async with self.db.session() as repos:
                conversation = await repos.conversations.get(conversation_id)
            if conversation is None:
                raise UnknownConversation(conversation_id)
            lease = await self._take_lease(conversation_id)
            try:
                return await self._admit(
                    self.ids.run_id(),
                    conversation_id,
                    conversation.user_id,
                    event,
                    lease,
                    trigger=event.kind,
                )
            except BaseException:
                await lease.release()
                raise

    async def _nothing_to_check(self, conversation_id: str) -> Answer | None:
        """`check_now` needs an intent that is waiting on the provider; otherwise 409 with
        the state of the conversation's latest intent (None if it never had one)."""
        async with self.db.session() as repos:
            # The reconciler's own test (`_WAITING`): a mismatch the provider recorded
            # (S65) is needs_attention but settled, and a run for it would only say so.
            if await repos.intents.awaiting_provider(conversation_id):
                return None
            async with repos.conn.execute(
                "SELECT intent_id, state FROM action_intents WHERE conversation_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ) as cur:
                row = await cur.fetchone()
        latest = {"intent_id": row[0], "state": row[1]} if row else None
        return Answer(409, {"error": "nothing_to_check", "intent": latest})

    async def _stored(self, client_action_id: str, route: str) -> Answer | None:
        async with self.db.session() as repos:
            found = await repos.client_requests.get(client_action_id)
        if found is None:
            return None
        if found.route != route or found.response is None:
            return Answer(422, {"error": "client_action_id_reused"})
        return Answer(int(found.response["status"]), dict(found.response["body"]))

    async def _remember(self, client_action_id: str, route: str, answer: Answer) -> None:
        now_ms = to_ms(self.clock.now())
        async with self.db.session() as repos:
            await repos.client_requests.record(
                client_action_id, route, {"status": answer.status, "body": answer.body}, now_ms
            )

    async def _dedupe(self, existing: Message, conversation_id: str, runs: Any) -> Admission:
        if existing.conversation_id != conversation_id or existing.run_id is None:
            raise ClientMsgIdReused(existing.client_msg_id)
        run = await runs.get(existing.run_id)
        routed: RoutedAs = "input"
        if run is not None and run.trigger == TRIGGERS["grant"]:
            routed = "grant"
        elif run is not None and run.trigger.endswith("resume"):
            routed = "resume"
        return Admission(run_id=existing.run_id, routed_as=routed, deduped=True)

    async def _admit(
        self,
        run_id: str,
        conversation_id: str,
        user_id: str,
        inbound: Inbound,
        lease: LeaseKeeper,
        *,
        routed: RoutedAs | None = None,
        trigger: str | None = None,
        user_text: str | None = None,
        client_msg_id: str | None = None,
        turn_no: int | None = None,
        client: dict[str, Any] | None = None,
    ) -> Admission:
        await self.world.sync_clock()  # one reading for the user message and the run's cards
        now = self.clock.now()
        real_model = self.model_settings.model_mode in ("live", "record")
        deadline_s = self.limits.live_run_deadline_s if real_model else self.limits.run_deadline_s
        counters = RunCounters(started_at=now, deadline_at=now + timedelta(seconds=deadline_s))
        run_input = {
            "thread_id": conversation_id,
            "user_id": user_id,
            "run_id": run_id,
            "inbound": inbound,
            "counters": counters,
            "node_failed": False,
        }
        graph_input, entered = await route_input(self.graph, conversation_id, run_input)
        routed_as: RoutedAs = routed or entered
        if trigger is None:
            trigger = TRIGGERS[routed_as]
        elif entered == "resume" and trigger != RECOVERY_TRIGGER:
            trigger = f"{trigger}_resume" if not trigger.endswith("resume") else trigger
        relative = trace_path(Path("."), conversation_id, run_id).as_posix()
        user_message_id = message_id_for_user(run_id) if user_text is not None else None
        async with self.db.transaction() as repos:
            if user_text is not None:
                assert user_message_id is not None
                message, created = await repos.messages.insert(
                    Message(
                        message_id=user_message_id,
                        conversation_id=conversation_id,
                        client_msg_id=client_msg_id,
                        role="user",
                        kind="text",
                        body={"type": "text", "text": user_text},
                        run_id=run_id,
                        turn_no=turn_no,
                        approval_id=None,
                        typed_grant_ok=None,
                        rendered_at=None,
                        created_at=to_ms(now),
                    )
                )
                if not created:
                    # Another process admitted the same client_msg_id between our check
                    # and this insert; the UNIQUE constraint decided, and that run is it.
                    assert message.run_id is not None
                    await lease.release()
                    return Admission(run_id=message.run_id, routed_as=routed_as, deduped=True)
            await repos.runs.start(
                run_id=run_id,
                conversation_id=conversation_id,
                trigger=trigger,
                now_ms=to_ms(now),
                trace_path=relative,
            )
            await repos.conversations.touch(conversation_id, to_ms(now))
        run = _Run(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=user_id,
            inbound=inbound,
            counters=counters,
            graph_input=graph_input,
            routed_as=routed_as,
            trigger=trigger,
            lease=lease,
            stream=self.broadcaster.open(run_id),
            deadline_s=deadline_s,
            user_message_id=user_message_id,
            client=client,
        )
        lease.start()
        self._active[conversation_id] = run_id
        task = asyncio.create_task(self._execute(run), name=f"run {run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Admission(run_id=run_id, routed_as=routed_as, deduped=False)

    async def start_recovery(
        self, conversation_id: str, inbound: Inbound, *, run_id: str | None = None
    ) -> Admission | None:
        """Start a recovery run (trigger `recovery`) on the thread, or None if it is busy.

        Only the reconciler calls this. The thread lease is taken like any run's, so a
        live run is never interrupted and a busy thread is simply tried again on the next
        tick; the startup pass has already taken over the dead process's leases.
        """
        if self._closing:
            raise ShuttingDown()
        async with self._locks.get(conversation_id):
            async with self.db.session() as repos:
                conversation = await repos.conversations.get(conversation_id)
            if conversation is None:
                raise UnknownConversation(conversation_id)
            try:
                lease = await self._take_lease(conversation_id)
            except RunInProgress:
                return None
            try:
                return await self._admit(
                    run_id or self.ids.run_id(),
                    conversation_id,
                    conversation.user_id,
                    inbound,
                    lease,
                    trigger=RECOVERY_TRIGGER,
                )
            except BaseException:
                await lease.release()
                raise

    # ------------------------------------------------------------------ execution

    def trace_file(self, conversation_id: str, run_id: str) -> Path:
        return trace_path(self.traces_dir, conversation_id, run_id)

    async def _execute(self, run: _Run) -> None:
        sink = JsonlSink(self.trace_file(run.conversation_id, run.run_id))
        tracer = Tracer(
            run_id=run.run_id,
            thread_id=run.conversation_id,
            sinks=[sink, StreamSink(run.stream.publish_record), self._dashboard(run)],
            now=self.clock.now,
        )
        outcome: RunOutcome = RunOutcome.INTERNAL_ERROR
        parked: str | None = None
        limit: str | None = None
        cancelled = False
        try:
            tracer.start_run(
                trigger=run.trigger,
                inbound_type=run.inbound.type,
                user_message_id=run.user_message_id,
                routed_as=run.routed_as,
                limits=self.limits.recorded(run.deadline_s),
                **(run.client or {}),
            )
            backstop = asyncio.timeout(self.limits.run_backstop_s)
            async with backstop:
                run.deps = self._deps(run, tracer)
                final = await invoke(self.graph, run.conversation_id, run.graph_input, run.deps)
            if final.state.outcome is None:
                raise RuntimeError("the run ended without an outcome")
            outcome = final.state.outcome
            parked = final.parked_on
            if final.state.result is None and isinstance(final.state.plan_result, Limit):
                limit = final.state.plan_result.limit
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            outcome = await self._fail(
                run,
                tracer,
                exc,
                backstop_hit=isinstance(exc, TimeoutError) and backstop.expired(),
            )
        finally:
            await self._memory_run_end(run, tracer, outcome)
            await self._finish(run, tracer, outcome, parked, limit)
        if cancelled:
            raise asyncio.CancelledError()

    async def _memory_run_end(self, run: _Run, tracer: Tracer, outcome: RunOutcome) -> None:
        """Suggestions and `last_used_at` after the outcome is final (N2B 2.8). Never raises."""
        try:
            turn = run.inbound.turn_no if isinstance(run.inbound, UserMessage) else None
            await memory_runend.run_end(
                run.deps, tracer, outcome, self.trace_file(run.conversation_id, run.run_id), turn
            )
        except Exception:
            log.exception("run %s: the memory run-end hook failed", run.run_id)

    def _dashboard(self, run: _Run) -> Sink:
        try:
            return self.trace_sink.for_run(run.run_id, run.conversation_id)
        except Exception:
            log.exception("run %s: no dashboard sink; the JSONL trace is unaffected", run.run_id)
            return NullSink()

    def _deps(self, run: _Run, tracer: Tracer) -> RunDeps:
        llm = LLMGateway(
            tracer=tracer,
            tools=PLANNER_TOOLS,
            settings=self.model_settings,
            limits=self.limits,
            transport=self.model_factory(),
            counters=run.counters,
            clock=self.clock,
        )
        tools = ToolGateway(
            self.world.client,
            tracer=tracer,
            clock=self.clock,
            counters=run.counters,
            limits=ToolLimits(max_tool_calls=self.limits.tool_calls_per_run),
        )
        reader = ReviewReader(
            tracer=tracer,
            counters=run.counters,
            clock=self.clock,
            db=self.db,
            mode=self.model_settings.model_mode,
            settings=self.reader_settings,
            limits=self.limits,
            transport=self.reader_factory(),
            cassette_path=self.model_settings.cassette_path,
        )
        return RunDeps(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            tracer=tracer,
            llm=llm,
            tools=tools,
            counters=run.counters,
            db=self.db,
            clock=self.clock,
            tz=self.tz,
            limits=self.limits,
            reader=reader,
            on_token=run.stream.publish_token,
            approval_settings=self.approval_settings,
            booking_settings=self.booking_settings,
            consequential=self.consequential,
            memory=self.memory,
        )

    async def _checkpointed_errand(self, run: _Run) -> Errand | None:
        """The thread's last committed errand, for the backstop notice. The checkpointer
        commits at step boundaries (`durability="sync"`), so a node cut mid-way leaves the
        previous step's state, never a half-written one. Any failure reading it falls back
        to the bare notice."""
        try:
            snapshot = await self.graph.aget_state(run_config(run.conversation_id))
            errand = snapshot.values.get("errand")
            return errand if isinstance(errand, Errand) else None
        except Exception:
            log.warning("run %s: could not read the checkpointed errand", run.run_id)
            return None

    async def _fail(
        self, run: _Run, tracer: Tracer, exc: BaseException, *, backstop_hit: bool = False
    ) -> RunOutcome:
        """The outer handler: an error record and a templated message, whatever broke.

        An exception or the backstop with an intent still open is not INTERNAL_ERROR: the
        provider may already have acted, so the run ends PENDING_VERIFICATION and the
        reconciler settles it (the critical section's one exit, 15.1). The 90 s backstop
        with no intent open is a time limit, not a bug: WALL_CLOCK_EXCEEDED.
        """
        if backstop_hit:
            log.warning("run %s hit the %s s backstop", run.run_id, self.limits.run_backstop_s)
        else:
            log.error("run %s failed", run.run_id, exc_info=exc)
        with contextlib.suppress(Exception):
            tracer.emit(
                "error",
                where="run_manager",
                exception_type=type(exc).__name__,
                message=str(exc)[:2000],
            )
        pending: PendingCheck | None = None
        try:
            pending = await critical.exit_critical_section(
                self.db,
                conversation_id=run.conversation_id,
                intent_ids=run.deps.intents if run.deps is not None else (),
                now_ms=to_ms(self.clock.now()),
                tracer=tracer,
                reason="backstop" if isinstance(exc, TimeoutError) else "exception",
            )
        except Exception:
            log.exception("run %s: could not settle its open intent", run.run_id)
        body: OutcomeNotice
        if pending is not None:
            body = critical.pending_notice(pending, self.tz)
            outcome = RunOutcome.PENDING_VERIFICATION
        elif backstop_hit:
            body = backstop_notice(await self._checkpointed_errand(run))
            outcome = RunOutcome.WALL_CLOCK_EXCEEDED
        else:
            body = internal_error(run.run_id)
            outcome = RunOutcome.INTERNAL_ERROR
        message = Message(
            message_id=message_id_for(run.run_id, "notice"),
            conversation_id=run.conversation_id,
            client_msg_id=None,
            role="assistant",
            kind="notice",
            body=body.model_dump(mode="json"),
            run_id=run.run_id,
            turn_no=run.inbound.turn_no if isinstance(run.inbound, UserMessage) else None,
            approval_id=None,
            typed_grant_ok=None,
            rendered_at=None,
            created_at=to_ms(self.clock.now()),
        )
        try:
            async with self.db.session() as repos:
                await repos.messages.upsert_assistant(message)
        except Exception:
            log.exception("run %s: could not write its %s message", run.run_id, outcome.value)
        return outcome

    async def _finish(
        self,
        run: _Run,
        tracer: Tracer,
        outcome: RunOutcome,
        parked: str | None,
        limit: str | None = None,
    ) -> None:
        if run.deps is not None:
            try:
                # Whatever happened, a run never leaves the user's consequential lock held.
                await run.deps.release_held()
            except Exception:
                log.exception("run %s: could not release the consequential lock", run.run_id)
        try:
            end = tracer.end_run(
                outcome=outcome.value,
                user_message_id=run.user_message_id,
                parked_approval_id=parked,
                limit=limit,
            )
            totals = end["totals"]
            async with self.db.session() as repos:
                await repos.runs.end(
                    run.run_id,
                    outcome=outcome.value,
                    totals=RunTotals(
                        model_calls=totals["model_calls"],
                        tool_calls=totals["tool_calls"],
                        tokens_in=totals["tokens_in"],
                        tokens_out=totals["tokens_out"],
                        cost_usd=totals["cost_usd"],
                    ),
                    now_ms=to_ms(self.clock.now()),
                )
        except Exception:
            log.exception("run %s: could not record its end", run.run_id)
        finally:
            self._active.pop(run.conversation_id, None)
            try:
                await run.lease.release()
            except Exception:
                log.exception("run %s: could not release its lease", run.run_id)
            self.broadcaster.finish(run.run_id)
            tracer.close()

    # ------------------------------------------------------------------ shutdown

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self, grace_s: float = 10.0) -> None:
        """Stop admitting, let running runs finish within `grace_s`, then cancel the rest
        (each still writes its message and `run.end`)."""
        self._closing = True
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _refusal(result: service.GrantResult) -> Answer:
    """Why a decision did not go through (8.3): never 'details changed' unless it did."""
    row = result.row
    outcome = result.outcome
    if outcome is service.GrantOutcome.NOT_FOUND or row is None:
        return Answer(404, {"error": "not_found"})
    if outcome is service.GrantOutcome.ALREADY:
        return Answer(409, {"error": "already_approved", "status": row.status})
    if outcome is service.GrantOutcome.EXPIRED:
        return Answer(
            409, {"error": "expired", "detail": "this card expired, refreshing the quote"}
        )
    if outcome is service.GrantOutcome.CHANGED:
        return Answer(
            409, {"error": "details_changed", "detail": "details changed, review the updated card"}
        )
    return Answer(
        409,
        {
            "error": "no_longer_valid",
            "status": row.status,
            "reason": row.invalidated_reason,
        },
    )
