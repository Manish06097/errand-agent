"""RunManager: admits user messages and runs each graph invocation as a detached task.

Admission, under the thread's lock and nothing else (6.6):
1. `client_msg_id` dedupe: a retried POST gets the run it already started. This comes
   before the active-run check, or a client retrying its own message mid-run would
   get a 409 for it.
2. one active run per thread: a different message gets 409 `run_in_progress`;
3. the thread lease (`thread:<id>`), renewed for the whole run; the run id is minted
   only after it, so a refused lease never burns an id;
4. the interrupt check, which decides plain input or `Command(resume=...)`;
5. the user message, the runs row and the task, then the lock is released.

A run is a task the manager owns, with a strong reference held until it finishes, so it
is never garbage-collected and never tied to the request that started it: a closed tab
or a dropped SSE connection cannot cancel it (ADR 0011). Each run gets its own tracer
(JSONL first, then the live stream) and its own model and tool gateways, which share
one `RunCounters`.

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

from errand.agent.deps import RunDeps
from errand.agent.graph import Graph, RoutedAs, invoke, route_input
from errand.agent.planner_tools import PLANNER_TOOLS
from errand.agent.templates import internal_error
from errand.api.locks import LeaseKeeper, ThreadLocks, thread_key
from errand.api.sse import Broadcaster, RunStream
from errand.clock import Clock, to_ms
from errand.config import LimitSettings, ModelSettings
from errand.domain.counters import RunCounters
from errand.domain.inbound import UserMessage
from errand.domain.outcomes import RunOutcome
from errand.ids import IdFactory, message_id_for, message_id_for_user
from errand.llm.gateway import LLMGateway
from errand.store.agent_db import AgentDB
from errand.store.repos.messages import Message
from errand.store.repos.runs import RunTotals
from errand.tools.gateway import ToolGateway, ToolLimits
from errand.tools.runtime import WorldAccess
from errand.trace.sinks.jsonl import JsonlSink, trace_path
from errand.trace.sinks.stream import StreamSink
from errand.trace.tracer import Tracer

log = logging.getLogger(__name__)

# Makes a scripted planner for one run (None in live mode: the gateway builds ChatAnthropic).
ModelFactory = Callable[[], BaseChatModel | None]
TRIGGERS: dict[RoutedAs, str] = {"input": "user_message", "resume": "user_message_resume"}


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


@dataclass(frozen=True)
class Admission:
    run_id: str
    routed_as: RoutedAs
    deduped: bool


@dataclass
class _Run:
    run_id: str
    conversation_id: str
    user_id: str
    inbound: UserMessage
    counters: RunCounters
    graph_input: dict[str, Any] | Command[Any]
    routed_as: RoutedAs
    lease: LeaseKeeper
    stream: RunStream


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
        self.broadcaster = broadcaster or Broadcaster()
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

    @property
    def clock(self) -> Clock:
        return self.world.clock

    def active_run(self, conversation_id: str) -> str | None:
        return self._active.get(conversation_id)

    # ------------------------------------------------------------------ admission

    async def submit_message(
        self, conversation_id: str, *, text: str, client_msg_id: str
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
            active = self._active.get(conversation_id)
            if active is not None:
                raise RunInProgress(active)
            # The holder names this admission, not the run: the run id is minted only
            # once the lease is ours, so a refused lease never burns one (BUG-M3-2).
            self._admissions += 1
            lease = await LeaseKeeper.acquire(
                self.db, thread_key(conversation_id), f"{self._holder}:a{self._admissions}"
            )
            if lease is None:
                raise RunInProgress(None)
            run_id = self.ids.run_id()
            try:
                return await self._admit(
                    run_id,
                    conversation_id,
                    conversation.user_id,
                    text,
                    client_msg_id,
                    turn_no,
                    lease,
                )
            except BaseException:
                await lease.release()
                raise

    async def _dedupe(self, existing: Message, conversation_id: str, runs: Any) -> Admission:
        if existing.conversation_id != conversation_id or existing.run_id is None:
            raise ClientMsgIdReused(existing.client_msg_id)
        run = await runs.get(existing.run_id)
        routed: RoutedAs = (
            "resume" if run is not None and run.trigger.endswith("resume") else "input"
        )
        return Admission(run_id=existing.run_id, routed_as=routed, deduped=True)

    async def _admit(
        self,
        run_id: str,
        conversation_id: str,
        user_id: str,
        text: str,
        client_msg_id: str,
        turn_no: int,
        lease: LeaseKeeper,
    ) -> Admission:
        now = self.clock.now()
        counters = RunCounters(
            started_at=now, deadline_at=now + timedelta(seconds=self.limits.run_deadline_s)
        )
        inbound = UserMessage(
            message_id=message_id_for_user(run_id),
            client_msg_id=client_msg_id,
            text=text,
            turn_no=turn_no,
        )
        run_input = {
            "thread_id": conversation_id,
            "user_id": user_id,
            "run_id": run_id,
            "inbound": inbound,
            "counters": counters,
        }
        graph_input, routed_as = await route_input(self.graph, conversation_id, run_input)
        relative = trace_path(Path("."), conversation_id, run_id).as_posix()
        async with self.db.transaction() as repos:
            message, created = await repos.messages.insert(
                Message(
                    message_id=inbound.message_id,
                    conversation_id=conversation_id,
                    client_msg_id=client_msg_id,
                    role="user",
                    kind="text",
                    body={"type": "text", "text": text},
                    run_id=run_id,
                    turn_no=turn_no,
                    approval_id=None,
                    typed_grant_ok=None,
                    rendered_at=None,
                    created_at=to_ms(now),
                )
            )
            if not created:
                # Another process admitted the same client_msg_id between our check and
                # this insert; the UNIQUE constraint decided, and that run is the answer.
                assert message.run_id is not None
                await lease.release()
                return Admission(run_id=message.run_id, routed_as=routed_as, deduped=True)
            await repos.runs.start(
                run_id=run_id,
                conversation_id=conversation_id,
                trigger=TRIGGERS[routed_as],
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
            lease=lease,
            stream=self.broadcaster.open(run_id),
        )
        lease.start()
        self._active[conversation_id] = run_id
        task = asyncio.create_task(self._execute(run), name=f"run {run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Admission(run_id=run_id, routed_as=routed_as, deduped=False)

    # ------------------------------------------------------------------ execution

    def trace_file(self, conversation_id: str, run_id: str) -> Path:
        return trace_path(self.traces_dir, conversation_id, run_id)

    async def _execute(self, run: _Run) -> None:
        sink = JsonlSink(self.trace_file(run.conversation_id, run.run_id))
        tracer = Tracer(
            run_id=run.run_id,
            thread_id=run.conversation_id,
            sinks=[sink, StreamSink(run.stream.publish_record)],
            now=self.clock.now,
        )
        outcome: RunOutcome = RunOutcome.INTERNAL_ERROR
        cancelled = False
        try:
            tracer.start_run(
                trigger=TRIGGERS[run.routed_as],
                inbound_type=run.inbound.type,
                user_message_id=run.inbound.message_id,
            )
            async with asyncio.timeout(self.limits.run_backstop_s):
                deps = self._deps(run, tracer)
                final = await invoke(self.graph, run.conversation_id, run.graph_input, deps)
            if final.outcome is None:
                raise RuntimeError("the run ended without an outcome")
            outcome = final.outcome
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            outcome = RunOutcome.INTERNAL_ERROR
            await self._fail(run, tracer, exc)
        finally:
            await self._finish(run, tracer, outcome)
        if cancelled:
            raise asyncio.CancelledError()

    def _deps(self, run: _Run, tracer: Tracer) -> RunDeps:
        llm = LLMGateway(
            tracer=tracer,
            tools=PLANNER_TOOLS,
            settings=self.model_settings,
            limits=self.limits,
            transport=self.model_factory(),
            counters=run.counters,
        )
        tools = ToolGateway(
            self.world.client,
            tracer=tracer,
            clock=self.clock,
            counters=run.counters,
            limits=ToolLimits(max_tool_calls=self.limits.tool_calls_per_run),
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
            on_token=run.stream.publish_token,
        )

    async def _fail(self, run: _Run, tracer: Tracer, exc: BaseException) -> None:
        """The outer handler: an error record and the INTERNAL_ERROR message, whatever broke."""
        log.error("run %s failed", run.run_id, exc_info=exc)
        with contextlib.suppress(Exception):
            tracer.emit(
                "error",
                where="run_manager",
                exception_type=type(exc).__name__,
                message=str(exc)[:2000],
            )
        body = internal_error(run.run_id)
        message = Message(
            message_id=message_id_for(run.run_id, "notice"),
            conversation_id=run.conversation_id,
            client_msg_id=None,
            role="assistant",
            kind="notice",
            body=body.model_dump(mode="json"),
            run_id=run.run_id,
            turn_no=run.inbound.turn_no,
            approval_id=None,
            typed_grant_ok=None,
            rendered_at=None,
            created_at=to_ms(self.clock.now()),
        )
        try:
            async with self.db.session() as repos:
                await repos.messages.upsert_assistant(message)
        except Exception:
            log.exception("run %s: could not write the INTERNAL_ERROR message", run.run_id)

    async def _finish(self, run: _Run, tracer: Tracer, outcome: RunOutcome) -> None:
        try:
            end = tracer.end_run(outcome=outcome.value, user_message_id=run.inbound.message_id)
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
