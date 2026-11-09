"""errand-api: the agent's HTTP surface (architecture 17.3).

Routes:
- `POST /api/conversations` creates a conversation;
- `GET /api/conversations/{id}` returns the transcript, the runs and a state snapshot
  read with `aget_state` (never the sync API, spike L6). Approval cards are rehydrated
  from `approvals.card_json` with their current status (M4);
- `POST /api/conversations/{id}/messages` admits a message and returns
  `{run_id, routed_as}` (`input`, `resume` or `grant`), or 409 `run_in_progress`. It
  takes `visible_card` and `client_composed_at`, captured by the client at Send, for the
  typed-grant check (8.3);
- `POST /api/approvals/{id}/decision` is the only button grant path, and decline (M4);
- `POST /api/messages/{id}/rendered` records the client's render ack (M4);
- `POST /api/conversations/{id}/actions` takes `select_slot` and `refresh_hold` (M4) and
  `check_now` (N1: 409 `nothing_to_check` when no intent waits on the provider),
  idempotent on `client_action_id`;
- `GET /api/runs/{id}/events` is the run's SSE stream with replay from `Last-Event-ID`;
  `card` and `card.update` events are trace records too, so they replay.
- `GET /api/health` also reports the Langfuse sink (on with its export failures, or off
  and why), from counters only, so it never waits on Langfuse.

The lifespan builds the optional Langfuse sink once (shut down last, after every run
has finished), opens agent.sqlite, the checkpointer and the world, builds the graph and
the RunManager, and on exit lets running runs finish (or cancels them after a grace
period, each still writing its message and `run.end`) before closing anything.

In scripted mode (the offline default) the planner is a `ScriptedChatModel` driven by
the policy named in ERRAND_SCRIPTED_POLICY, by default the eval suite's reference
policy, and the review reader one driven by ERRAND_SCRIPTED_READER (the keyword test
double), so `make dev` works end to end with curl and no API key.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand import faults
from errand.agent.brief_ops import INVALIDATES, missing
from errand.agent.graph import build_graph, run_config
from errand.agent.state import ThreadState
from errand.api import dev_routes, followup_routes, web_routes
from errand.api.memory_routes import (
    MEMORY_ACTIONS,
    memory_chat_action,
    resolve_memory_body,
)
from errand.api.memory_routes import mount as mount_memory_routes
from errand.api.reconciler import Reconciler
from errand.api.runs import (
    Answer,
    ClientMsgIdReused,
    ModelFactory,
    RunInProgress,
    RunManager,
    ShuttingDown,
    UnknownApproval,
    UnknownConversation,
)
from errand.api.sse import stream_events
from errand.approvals import service
from errand.approvals.typed_grant import VisibleCard as VisibleCardRef
from errand.clock import AGENT_CLOCK_HEADER, IST, encode_clock_header, reading_of, to_ms
from errand.config import (
    ApprovalSettings,
    BookingSettings,
    FollowupSettings,
    LangfuseKeys,
    LimitSettings,
    MemorySettings,
    ModelSettings,
    PathSettings,
    ReconcilerSettings,
    TraceSettings,
)
from errand.domain.brief import BriefField
from errand.domain.inbound import SystemEvent
from errand.domain.messages import MEMORY_BODY_TYPES, MessageBody
from errand.followup.watcher import Watcher
from errand.ids import IdFactory, SequentialIds, Uuid7Ids
from errand.llm.scripted import Policy, ScriptedChatModel
from errand.memory.hmac_key import load_or_create
from errand.memory.registry import PREFILL_FIELD
from errand.memory.scrub import scrub_brief
from errand.memory.service import MemoryService
from errand.store.agent_db import AgentDB
from errand.store.repos.users import User
from errand.tools.runtime import WorldAccess, open_in_process, over_http
from errand.trace.masked import MaskUnavailable, masker_for
from errand.trace.sinks import ProcessSink, make_sink, off_reason

SERVICE = "errand-api"
log = logging.getLogger(__name__)


class ServeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_API_")

    host: str = "127.0.0.1"
    port: int = 8000


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    # The world over HTTP (make dev); unset means the in-process world at data/world.sqlite.
    world_url: str | None = None
    # module:attribute of the scripted planner policy used in scripted mode.
    scripted_policy: str = "evals.policies.reference:policy"
    # module:attribute of the scripted review reader used in scripted mode. The default
    # keyword double is a test stand-in, not a defence.
    scripted_reader: str = "evals.policies.reader:keyword"
    # sim: c_0001, r_0001 ...; live: uuid7. Default follows the model mode.
    id_mode: Literal["sim", "live"] | None = None
    user_id: str = "u_demo"


@dataclass
class AppConfig:
    """What `create_app` can be given instead of reading the environment (tests, evals)."""

    data_dir: Path | None = None
    world: WorldAccess | None = None
    model_factory: ModelFactory | None = None
    reader_factory: ModelFactory | None = None
    ids: IdFactory | None = None
    model_settings: ModelSettings | None = None
    limits: LimitSettings | None = None
    tz: tzinfo = IST
    user_id: str | None = None
    approval_settings: ApprovalSettings | None = None
    booking_settings: BookingSettings | None = None
    # The watcher's interval (0 turns it off); evals and tests tick it by hand.
    followup_settings: FollowupSettings | None = None
    reconciler: ReconcilerSettings | None = None
    # Tests replace the per-user consequential lock (the S28 backstop test runs two
    # executes with no lock at all).
    consequential: Any = None
    # Builds the process's dashboard sink once, in the lifespan (tests inject exporters).
    sink_factory: Callable[[], ProcessSink] = make_sink


class VisibleCard(BaseModel):
    """The card the user was looking at when they pressed Send (8.3)."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=100)
    fingerprint: str = Field(min_length=1, max_length=100)
    message_id: str = Field(min_length=1, max_length=100)


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_msg_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=200_000)
    # ISO 8601 with an offset, the client's clock at Send (server-synced, 5.7).
    client_composed_at: str | None = Field(default=None, max_length=40)
    visible_card: VisibleCard | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "decline"]
    fingerprint: str = Field(min_length=1, max_length=100)
    client_action_id: str = Field(min_length=1, max_length=100)


class RenderedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ts: str = Field(min_length=1, max_length=40)


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str = Field(min_length=1, max_length=100)
    kind: Literal[
        "select_slot",
        "refresh_hold",
        "check_now",
        "widen_followup",
        "dismiss_followup",
        "keep_memory",
        "confirm_forget",
        "update_memory",
        "save_memory",
    ]
    payload: dict[str, str] = Field(default_factory=dict)


class ApprovalStatusOut(BaseModel):
    """The live status of the approval a card or reminder is about, beside its body."""

    status: str
    reason: str | None = None
    fingerprint: str
    expires_at: int


class MessageOut(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    kind: str
    body: MessageBody
    run_id: str | None = None
    turn_no: int | None = None
    created_at: int
    approval_id: str | None = None
    typed_grant_ok: bool | None = None
    rendered_at: int | None = None
    approval: ApprovalStatusOut | None = None


class RunOut(BaseModel):
    run_id: str
    status: str
    outcome: str | None = None
    trigger: str | None = None


class ClockOut(BaseModel):
    """The agent clock (5.7): what `client_composed_at` must be measured against."""

    mode: Literal["sim", "system_offset"]
    now_ms: int
    offset_ms: int


class ConversationOut(BaseModel):
    conversation_id: str
    messages: list[MessageOut]
    runs: list[RunOut]
    active_run_id: str | None = None
    state: dict[str, Any] | None = None
    clock: ClockOut


def parse_client_ms(value: str | None) -> int | None:
    """A client timestamp (ISO 8601 with an offset) as epoch ms; None if absent or bad."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return to_ms(moment)


def load_policy(spec: str) -> Policy:
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    policy: Policy = getattr(module, attr or "policy")
    return policy


def scripted_factory(policy: Policy) -> ModelFactory:
    def make() -> ScriptedChatModel:
        return ScriptedChatModel(script=policy)

    return make


async def _highest(db: AgentDB, table: Literal["conversations", "runs"]) -> int:
    """The highest numeric suffix among the table's sim ids (`c_0007` gives 7).

    Not COUNT(*): an admission that mints a run id and then does not insert it (a lost
    client_msg_id race, an exception before the insert) leaves a gap, and a counter
    seeded from the row count would then hand out an id that exists (BUG-M3-2). Ids
    that are not all digits after the prefix (uuid7 ids from a live run) are skipped.
    """
    column = "conversation_id" if table == "conversations" else "run_id"
    sql = (
        f"SELECT MAX(CAST(SUBSTR({column}, 3) AS INTEGER)) FROM {table} "
        f"WHERE LENGTH({column}) > 2 AND SUBSTR({column}, 3) NOT GLOB '*[^0-9]*'"
    )
    async with db.session() as repos, repos.conn.execute(sql) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def card_invalidating_keys() -> frozenset[str]:
    """Memory keys whose change always discards a pending card, read from the brief's
    INVALIDATES table: the key's brief field says `approval` in its `always` set."""
    return frozenset(
        key
        for key, name in PREFILL_FIELD.items()
        if "approval" in INVALIDATES[BriefField(name)].always
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = AgentSettings()
        faults.configure()  # an unknown fault point refuses to start (9.9)
        model_settings = config.model_settings or ModelSettings()
        limits = config.limits or LimitSettings()
        data_dir = config.data_dir or PathSettings().data_dir
        user_id = config.user_id or settings.user_id
        async with AsyncExitStack() as stack:
            # First in, so last out: runs are finished before the dashboard is flushed.
            trace_sink = config.sink_factory()
            stack.push_async_callback(asyncio.to_thread, trace_sink.shutdown)
            app.state.trace_sink = trace_sink
            db = await AgentDB.open(data_dir / "agent.sqlite")
            stack.push_async_callback(db.close)
            async with db.session() as repos:
                await repos.users.upsert(
                    User(user_id=user_id, tz="Asia/Kolkata", display_name="Demo")
                )
            saver = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(data_dir / "checkpoints.sqlite"))
            )
            world = config.world
            if world is None:
                if settings.world_url:
                    world = await over_http(settings.world_url)
                else:
                    world = open_in_process(data_dir / "world.sqlite")
                stack.push_async_callback(world.aclose)
            ids = config.ids
            if ids is None:
                id_mode = settings.id_mode or (
                    "sim" if model_settings.model_mode in ("scripted", "replay") else "live"
                )
                ids = (
                    SequentialIds(
                        conversations=await _highest(db, "conversations"),
                        runs=await _highest(db, "runs"),
                    )
                    if id_mode == "sim"
                    else Uuid7Ids()
                )
            factory = config.model_factory
            if factory is None:
                if model_settings.model_mode == "scripted":
                    factory = scripted_factory(load_policy(settings.scripted_policy))
                else:
                    factory = _no_transport
            reader_factory = config.reader_factory
            if reader_factory is None:
                if model_settings.model_mode == "scripted":
                    reader_factory = scripted_factory(load_policy(settings.scripted_reader))
                else:
                    reader_factory = _no_transport
            manager = RunManager(
                graph=build_graph(saver),
                db=db,
                world=world,
                ids=ids,
                traces_dir=data_dir / "traces",
                tz=config.tz,
                model_settings=model_settings,
                limits=limits,
                model_factory=factory,
                approval_settings=config.approval_settings,
                booking_settings=config.booking_settings,
                consequential=config.consequential,
                reader_factory=reader_factory,
                trace_sink=trace_sink,
            )
            app.state.manager = manager
            app.state.user_id = user_id
            app.state.memory = MemoryService(
                db,
                lambda: to_ms(manager.clock.now()),
                load_or_create(data_dir / MemorySettings().hmac_key_file),
                card_keys=card_invalidating_keys(),
            )
            manager.memory = app.state.memory

            async def start_followup(conversation_id: str, event: SystemEvent) -> None:
                await manager.submit_system(conversation_id, event)

            watcher = Watcher(
                db=db,
                world=world,
                tz=config.tz,
                traces_dir=data_dir / "traces",
                start=start_followup,
                interval_s=config.followup_settings.watch_interval_s
                if config.followup_settings is not None
                else FollowupSettings().watch_interval_s,
                extra_users=lambda: [user_id],
            )
            app.state.watcher = watcher
            manager.followup_watcher = watcher  # the reconciler's follow-up pass reads it
            reconciler = Reconciler(manager, config.reconciler or ReconcilerSettings())
            app.state.reconciler = reconciler
            await reconciler.startup()  # before the first request is served (9.7)
            reconciler.start_periodic()
            watcher.start_task()
            try:
                yield
            finally:
                await watcher.stop()
                await reconciler.stop()
                await manager.shutdown()

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    mount_memory_routes(app)

    @app.middleware("http")
    async def agent_clock_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """`X-Agent-Clock: <mode>; now_ms=...; offset_ms=...` on every /api response, the
        clock a client syncs to for `client_composed_at` (17.3), mirroring X-World-Clock."""
        response = await call_next(request)
        manager = getattr(request.app.state, "manager", None)
        if manager is not None:
            reading = reading_of(manager.clock)
            response.headers[AGENT_CLOCK_HEADER] = encode_clock_header(
                reading.mode, reading.now_ms, reading.offset_ms
            )
        return response

    def manager_of(request: Request) -> RunManager:
        manager: RunManager = request.app.state.manager
        return manager

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        # status() reads counters only: no auth_check, nothing that can block on Langfuse.
        sink: ProcessSink | None = getattr(request.app.state, "trace_sink", None)
        if sink is not None:
            langfuse = sink.status()
        else:
            langfuse = {
                "state": "off",
                "reason": off_reason(TraceSettings(), LangfuseKeys()) or "not_started",
            }
        watcher: Watcher | None = getattr(request.app.state, "watcher", None)
        health: dict[str, Any] = {"status": "ok", "service": SERVICE, "langfuse": langfuse}
        if watcher is not None:
            # Lag since the last tick that listed every user's bookings (16.5).
            health["watcher"] = (
                {
                    "state": "on",
                    "lag_s": watcher.health.lag_s(manager_of(request).clock.now()),
                    "failing_users": watcher.health.failing_users,
                }
                if watcher.interval_s > 0
                else {"state": "off"}
            )
        return health

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(request: Request) -> dict[str, str]:
        manager = manager_of(request)
        conversation_id = manager.ids.conversation_id()
        now = to_ms(manager.clock.now())
        async with manager.db.session() as repos:
            await repos.conversations.create(
                conversation_id=conversation_id, user_id=request.app.state.user_id, now_ms=now
            )
        return {"conversation_id": conversation_id}

    @app.get(
        "/api/conversations/{conversation_id}",
        response_model=ConversationOut,
        responses={404: {"description": "unknown conversation"}},
    )
    async def get_conversation(conversation_id: str, request: Request) -> Any:
        manager = manager_of(request)
        async with manager.db.session() as repos:
            conversation = await repos.conversations.get(conversation_id)
            if conversation is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
        # The sweep first, so a lapsed card is served as expired, not pending.
        await service.live_approval(
            manager.db, conversation_id, to_ms(manager.clock.now()), manager.approval_policy
        )
        async with manager.db.session() as repos:
            messages = await repos.messages.list_for_conversation(conversation_id)
            runs = await repos.runs.list_for_conversation(conversation_id)
            approvals = {
                a.approval_id: a
                for a in await repos.approvals.list_for_conversation(conversation_id)
            }
        snapshot = await manager.graph.aget_state(run_config(conversation_id))
        async with manager.db.session() as repos:
            eligible = await repos.memories.eligible_ids(conversation.user_id)
            memory_rows = (
                {r.memory_id: r for r in await repos.memories.for_user(conversation.user_id)}
                if any(m.body.get("type") in MEMORY_BODY_TYPES for m in messages)
                else {}
            )
        return {
            "conversation_id": conversation_id,
            "messages": [_message_view(m, approvals, memory_rows) for m in messages],
            "runs": [
                {"run_id": r.run_id, "status": r.status, "outcome": r.outcome, "trigger": r.trigger}
                for r in runs
            ],
            "active_run_id": manager.active_run(conversation_id),
            "state": _state_view(
                snapshot.values, snapshot.next, bool(snapshot.interrupts), eligible
            ),
            "clock": reading_of(manager.clock).__dict__,
        }

    @app.post("/api/conversations/{conversation_id}/messages", response_model=None)
    async def post_message(conversation_id: str, body: NewMessage, request: Request) -> Any:
        manager = manager_of(request)
        card = body.visible_card
        try:
            admission = await manager.submit_message(
                conversation_id,
                text=body.text,
                client_msg_id=body.client_msg_id,
                client_composed_at=body.client_composed_at,
                composed_at_ms=parse_client_ms(body.client_composed_at),
                visible_card=None
                if card is None
                else VisibleCardRef(card.approval_id, card.fingerprint, card.message_id),
            )
        except UnknownConversation:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except RunInProgress as exc:
            return JSONResponse({"error": "run_in_progress", "run_id": exc.run_id}, status_code=409)
        except ClientMsgIdReused:
            return JSONResponse({"error": "client_msg_id_reused"}, status_code=422)
        except ShuttingDown:
            return JSONResponse({"error": "shutting_down"}, status_code=503)
        status = 200 if admission.deduped else 202
        return JSONResponse(
            {"run_id": admission.run_id, "routed_as": admission.routed_as}, status_code=status
        )

    @app.post("/api/approvals/{approval_id}/decision", response_model=None)
    async def approval_decision(approval_id: str, body: DecisionIn, request: Request) -> Any:
        manager = manager_of(request)
        try:
            answer = await manager.submit_decision(
                approval_id,
                decision=body.decision,
                fingerprint=body.fingerprint,
                client_action_id=body.client_action_id,
            )
        except UnknownApproval:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except RunInProgress as exc:
            return JSONResponse({"error": "run_in_progress", "run_id": exc.run_id}, status_code=409)
        except ShuttingDown:
            return JSONResponse({"error": "shutting_down"}, status_code=503)
        return _answer(answer)

    @app.post("/api/messages/{message_id}/rendered", response_model=None)
    async def message_rendered(message_id: str, body: RenderedIn, request: Request) -> Any:
        manager = manager_of(request)
        client_ms = parse_client_ms(body.client_ts)
        if client_ms is None:
            return JSONResponse({"error": "bad_client_ts"}, status_code=422)
        async with manager.db.session() as repos:
            message = await repos.messages.get(message_id)
            if message is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            # The first ack wins; a later one (a re-render) changes nothing.
            await repos.messages.mark_rendered(message_id, client_ms)
            message = await repos.messages.get(message_id)
        assert message is not None
        return {"message_id": message_id, "rendered_at": message.rendered_at}

    @app.post("/api/conversations/{conversation_id}/actions", response_model=None)
    async def post_action(conversation_id: str, body: ActionIn, request: Request) -> Any:
        if body.kind in MEMORY_ACTIONS:
            return await memory_chat_action(
                request, conversation_id, body.kind, body.client_action_id, body.payload
            )
        manager = manager_of(request)
        try:
            answer = await manager.submit_action(
                conversation_id,
                client_action_id=body.client_action_id,
                kind=body.kind,
                payload=body.payload,
            )
        except UnknownConversation:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except RunInProgress as exc:
            return JSONResponse({"error": "run_in_progress", "run_id": exc.run_id}, status_code=409)
        except ShuttingDown:
            return JSONResponse({"error": "shutting_down"}, status_code=503)
        return _answer(answer)

    @app.get("/api/runs/{run_id}/events", response_class=EventSourceResponse, response_model=None)
    async def run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None),
        after: int | None = None,
    ) -> AsyncIterator[ServerSentEvent]:
        manager = manager_of(request)
        async with manager.db.session() as repos:
            run = await repos.runs.get(run_id)
        if run is None or run.trace_path is None:
            yield ServerSentEvent(data={"error": "not_found", "run_id": run_id}, event="error")
            return
        start = after if after is not None else _parse_last_id(last_event_id)
        try:
            masker = masker_for(manager.traces_dir.parent)
        except MaskUnavailable:
            yield ServerSentEvent(data={"error": "mask_unavailable"}, event="error")
            return
        async for event in stream_events(
            trace_file=manager.traces_dir / run.trace_path,
            stream=manager.broadcaster.get(run_id),
            last_event_id=start,
            masker=masker,
        ):
            yield event

    web_routes.register(app)
    followup_routes.register(app)
    dev_routes.register(app)
    return app


def _no_transport() -> None:
    return None


def _answer(answer: Answer) -> JSONResponse:
    return JSONResponse(answer.body, status_code=answer.status)


def _message_view(
    m: Any, approvals: dict[str, Any], memory_rows: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A transcript row as the client renders it. An approval card's body is the stored
    `card_json` (what code fingerprinted), and its current status rides beside it."""
    body = m.body
    approval = approvals.get(m.approval_id) if m.approval_id else None
    if approval is not None and body.get("type") == "action":
        body = approval.card
    if body.get("type") in MEMORY_BODY_TYPES:
        body = resolve_memory_body(body, memory_rows or {})
    view: dict[str, Any] = {
        "message_id": m.message_id,
        "role": m.role,
        "kind": m.kind,
        "body": body,
        "run_id": m.run_id,
        "turn_no": m.turn_no,
        "created_at": m.created_at,
        "approval_id": m.approval_id,
        "typed_grant_ok": m.typed_grant_ok,
        "rendered_at": m.rendered_at,
    }
    if approval is not None:
        view["approval"] = {
            "status": approval.status,
            "reason": approval.invalidated_reason,
            "fingerprint": approval.fingerprint,
            "expires_at": approval.expires_at,
        }
    return view


def _parse_last_id(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return 0


def _state_view(
    values: dict[str, Any], next_nodes: tuple[str, ...], interrupted: bool, eligible: frozenset[str]
) -> Any:
    if not values:
        return None
    state = ThreadState.model_validate(values)
    errand = state.errand
    if errand is not None:
        # A field sourced from a memory that is no longer eligible is not shown (decision 10).
        # Deliberately coarse: a window with a stale bound is hidden whole until the next turn's
        # apply_memory re-resolves it (this read has no clock to re-resolve with).
        brief, _ = scrub_brief(errand.brief, eligible)
        errand = errand.model_copy(update={"brief": brief})
    return {
        "run_id": state.run_id,
        "outcome": state.outcome,
        "next": list(next_nodes),
        "interrupted": interrupted,
        "counters": state.counters.model_dump(mode="json"),
        "errand": None
        if errand is None
        else {
            **errand.model_dump(mode="json"),
            "missing": [f.value for f in missing(errand.brief, errand.kind)],
        },
    }


app = create_app()


def main() -> None:
    settings = ServeSettings()
    uvicorn.run(app, host=settings.host, port=settings.port)
