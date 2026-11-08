"""errand-api: the agent's HTTP surface (architecture 17.3).

Routes in M3:
- `POST /api/conversations` creates a conversation;
- `GET /api/conversations/{id}` returns the transcript, the runs and a state snapshot
  read with `aget_state` (never the sync API, spike L6);
- `POST /api/conversations/{id}/messages` admits a message and returns
  `{run_id, routed_as}`, or 409 `run_in_progress`;
- `GET /api/runs/{id}/events` is the run's SSE stream with replay from `Last-Event-ID`;
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
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand.agent.brief_ops import missing
from errand.agent.graph import build_graph, run_config
from errand.agent.state import ThreadState
from errand.api.runs import (
    ClientMsgIdReused,
    ModelFactory,
    RunInProgress,
    RunManager,
    ShuttingDown,
    UnknownConversation,
)
from errand.api.sse import stream_events
from errand.clock import IST, to_ms
from errand.config import LangfuseKeys, LimitSettings, ModelSettings, PathSettings, TraceSettings
from errand.ids import IdFactory, SequentialIds, Uuid7Ids
from errand.llm.scripted import Policy, ScriptedChatModel
from errand.store.agent_db import AgentDB
from errand.store.repos.users import User
from errand.tools.runtime import WorldAccess, open_in_process, over_http
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
    # Builds the process's dashboard sink once, in the lifespan (tests inject exporters).
    sink_factory: Callable[[], ProcessSink] = make_sink


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_msg_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=200_000)
    client_composed_at: str | None = None
    visible_card: dict[str, str] | None = None  # used from M4 (typed grants)


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


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or AppConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = AgentSettings()
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
                reader_factory=reader_factory,
                trace_sink=trace_sink,
            )
            app.state.manager = manager
            app.state.user_id = user_id
            try:
                yield
            finally:
                await manager.shutdown()

    app = FastAPI(title=SERVICE, lifespan=lifespan)

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
        return {"status": "ok", "service": SERVICE, "langfuse": langfuse}

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

    @app.get("/api/conversations/{conversation_id}", response_model=None)
    async def get_conversation(conversation_id: str, request: Request) -> Any:
        manager = manager_of(request)
        async with manager.db.session() as repos:
            conversation = await repos.conversations.get(conversation_id)
            if conversation is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            messages = await repos.messages.list_for_conversation(conversation_id)
            runs = await repos.runs.list_for_conversation(conversation_id)
        snapshot = await manager.graph.aget_state(run_config(conversation_id))
        return {
            "conversation_id": conversation_id,
            "messages": [
                {
                    "message_id": m.message_id,
                    "role": m.role,
                    "kind": m.kind,
                    "body": m.body,
                    "run_id": m.run_id,
                    "turn_no": m.turn_no,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
            "runs": [
                {"run_id": r.run_id, "status": r.status, "outcome": r.outcome, "trigger": r.trigger}
                for r in runs
            ],
            "active_run_id": manager.active_run(conversation_id),
            "state": _state_view(snapshot.values, snapshot.next, bool(snapshot.interrupts)),
        }

    @app.post("/api/conversations/{conversation_id}/messages", response_model=None)
    async def post_message(conversation_id: str, body: NewMessage, request: Request) -> Any:
        manager = manager_of(request)
        try:
            admission = await manager.submit_message(
                conversation_id, text=body.text, client_msg_id=body.client_msg_id
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
        async for event in stream_events(
            trace_file=manager.traces_dir / run.trace_path,
            stream=manager.broadcaster.get(run_id),
            last_event_id=start,
        ):
            yield event

    return app


def _no_transport() -> None:
    return None


def _parse_last_id(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return 0


def _state_view(values: dict[str, Any], next_nodes: tuple[str, ...], interrupted: bool) -> Any:
    if not values:
        return None
    state = ThreadState.model_validate(values)
    errand = state.errand
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
