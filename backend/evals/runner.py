"""The scenario runner: the real app over httpx's ASGI transport (architecture 18.1).

Each scenario gets a fresh directory holding all three SQLite files (agent.sqlite,
checkpoints.sqlite, world.sqlite) and the traces, an in-process world on a SimClock,
and the scripted planner. Everything goes through the HTTP API the web app uses:
messages are POSTed, outcomes are read from the run's SSE stream, and the simulated
user reads its questions from the transcript. Invariants then read the three records.

`Harness` is the reusable part; API tests use it directly.

The simulated user's client (M4) behaves like the web client: it reads cards from
`GET /api/conversations/{id}` and the run's SSE `card` events, acks every card render
(`POST /api/messages/{id}/rendered`) with its clock, and attaches `visible_card` and
`client_composed_at` to what it types. Its clock is the simulated clock, nudged one
second forward before each ack and each message, so a request, its render and a typed
reply are strictly ordered and every run of a scenario sees the same times.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from errand.api.app import AppConfig, create_app, scripted_factory
from errand.api.runs import ModelFactory
from errand.clock import AGENT_CLOCK_HEADER, IST, ClockMode, from_ms, parse_clock_header
from errand.config import (
    ApprovalSettings,
    BookingSettings,
    FollowupSettings,
    LimitSettings,
    ModelSettings,
)
from errand.domain.brief import BriefField
from errand.domain.messages import ClarifyCard
from errand.ids import SequentialIds
from errand.llm.scripted import Policy
from errand.tools.inspector import InProcessInspector, row_to_json
from errand.tools.runtime import in_process
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig, parse_duration
from evals import invariants, memory_steps
from evals.dsl import (
    Advance,
    Answer,
    Approve,
    ApproveConcurrently,
    CheckNow,
    ClickAction,
    ClickFollowup,
    ClickSlot,
    Decline,
    Dedupe,
    ExpectApprovals,
    ExpectBookCalls,
    ExpectBooking,
    ExpectBrief,
    ExpectCancel,
    ExpectCard,
    ExpectFollowup,
    ExpectLimit,
    ExpectMove,
    ExpectNoCard,
    ExpectNoSideEffects,
    ExpectNotice,
    ExpectNotification,
    ExpectNotInRequests,
    ExpectOptions,
    ExpectOutcome,
    ExpectPatch,
    ExpectPlanner,
    ExpectQuestionFor,
    ExpectRank,
    ExpectReader,
    ExpectRows,
    ExpectSearchMatchesBrief,
    ExpectShortlist,
    ExpectToolErrors,
    ExpectTrace,
    ExpectTranscript,
    ExpectWorld,
    IfDedupe,
    OpenFollowup,
    Post,
    ReconcilerTick,
    RefreshHold,
    Restart,
    Say,
    Scenario,
    Step,
    SwitchConversation,
    Twin,
    TypeConfirm,
    Until,
    WatcherTick,
    WorldEvent,
)
from evals.policies import context as context_policies
from evals.policies import hostile, reference
from evals.policies import limits as limits_policies
from evals.policies import reader as reader_doubles
from evals.replay import ReplayRun
from evals.simuser import SimUser, SimUserFailure

APPROVAL_CARDS = ("action", "reminder")

EVALS = Path(__file__).resolve().parent
OUT = EVALS / "out"
# The unit tests that run scenarios point this elsewhere (tests/conftest.py), so parallel
# workers never share backend/evals/out. make evals leaves it unset.
TRACE_DIR_ENV = "ERRAND_EVAL_TRACE_DIR"
# The tests that run a process scenario pin its API port to their worker's block
# (tests/conftest.py worker_port); `make evals` leaves it unset and takes a free one.
API_PORT_ENV = "ERRAND_EVAL_API_PORT"
BASE_URL = "http://errand.test"

PLANNERS: dict[str, Policy] = {
    "scripted:reference": reference.policy,
    "scripted:injection_follower": hostile.injection_follower,
    "scripted:budget_poisoner": hostile.budget_poisoner,
    "hostile:direct_book": hostile.direct_book_policy,
    "hostile:false_claim": hostile.false_claim_policy,
    "hostile:paraphrase_claim": hostile.paraphrase_claim_policy,
    "hostile:cross_service_cite": hostile.cross_service_cite_policy,
    "hostile:loop_forever": limits_policies.loop_forever_policy,
    "hostile:loop_forever_x3": limits_policies.loop_forever_x3_policy,
    "hostile:matching_quote": hostile.matching_quote_policy,
    "hostile:no_evidence": hostile.no_evidence_policy,
    "hostile:no_day_part": hostile.no_day_part_policy,
    "hostile:reask": hostile.reask_policy,
    "scripted:memory": context_policies.memory_policy,
    "scripted:growing": context_policies.growing_policy,
}
SIDE_EFFECT_OPS = ("hold_slot", "book", "cancel", "reschedule")


@dataclass(frozen=True)
class SseEvent:
    event: str
    id: str | None
    data: Any


def parse_sse(text: str) -> list[SseEvent]:
    events: list[SseEvent] = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        name, ident, data = "message", None, []
        for line in chunk.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                name = value
            elif key == "id":
                ident = value
            elif key == "data":
                data.append(value)
        if data:
            events.append(SseEvent(name, ident, json.loads("\n".join(data))))
    return events


@dataclass
class RunView:
    run_id: str
    outcome: str | None
    events: list[SseEvent]
    messages: list[dict[str, Any]]  # this run's assistant messages, from the transcript

    def card(self) -> ClarifyCard | None:
        for m in self.messages:
            if m["body"].get("type") == "clarify":
                return ClarifyCard.model_validate(m["body"])
        return None

    def notice(self) -> dict[str, Any] | None:
        return next((m["body"] for m in self.messages if m["body"].get("type") == "notice"), None)

    def last(self) -> dict[str, Any] | None:
        return self.messages[-1] if self.messages else None

    def action_card(self) -> dict[str, Any] | None:
        return next((m for m in self.messages if m["body"].get("type") == "action"), None)

    def card_events(self) -> list[dict[str, Any]]:
        return [e.data for e in self.events if e.event == "card"]


class Harness:
    """One app, one in-process world, one conversation, all in `root`."""

    def __init__(
        self,
        root: Path,
        world: ScenarioConfig,
        policy: Policy,
        model_factory: ModelFactory | None = None,
        reader: Policy = reader_doubles.keyword,
        reader_factory: ModelFactory | None = None,
        limits: LimitSettings | None = None,
        consequential: Any = None,
        clock_mode: ClockMode = "sim",
        model_settings: ModelSettings | None = None,
        booking: BookingSettings | None = None,
        memory_seed: str | None = None,
        hooks: list[memory_steps.CallHook] | None = None,
    ) -> None:
        self.memory_seed = memory_seed
        self.hooks = hooks or []
        self.inactive_pairs: list[dict[str, Any]] = []
        self._fired: set[int] = set()
        self.runs_started = 0
        self.model_settings = model_settings or ModelSettings(model_mode="scripted")
        self.clock_mode = clock_mode
        # The provider's dedupe is declared once, in the scenario's world block; the agent's
        # `booking.provider_dedupes_keys` follows it. The retry budget comes from the
        # environment (ERRAND_BOOKING_MAX_SAME_KEY_RETRIES), like any setting.
        self.booking = booking or BookingSettings().model_copy(
            update={"provider_dedupes_keys": world.world.enforce_idempotency}
        )
        self.root = root
        self.limits = limits or LimitSettings()
        self.world_config = world
        self.policy = policy
        self._consequential = consequential
        self.model_factory = model_factory or scripted_factory(policy)
        self.reader = reader
        self.reader_factory = reader_factory or scripted_factory(reader)
        self.host = WorldHost(root / "world.sqlite")
        self.inspector = InProcessInspector(self.host)
        self.client: httpx.AsyncClient
        self.conversation_id = ""
        self.conversations: dict[str, str] = {}
        self._stack = AsyncExitStack()
        self._msg_seq = 0
        self._action_seq = 0

    async def _seed_memory(self) -> None:
        """Build the scenario's seed into the data directory before the app opens it."""
        from errand.seed.memory_demo import build_demo
        from errand.seed.memory_rich import build_rich

        if self.memory_seed == "memory_demo":
            seeded = await build_demo(self.root)
        else:
            seeded, _ = await build_rich(self.root)
        async with seeded.db.session() as repos:
            rows = await repos.memories.for_user("u_demo")
        self.inactive_pairs = [
            {"key": r.key, "value": r.value, "status": r.status}
            for r in rows
            if r.status in ("pending_confirmation", "rejected") and r.value is not None
        ]
        await seeded.db.close()

    async def _before_model_call(self, n: int) -> None:
        """Harness-side triggers (`hooks`): a real DELETE just before the Nth model call."""
        for i, hook in enumerate(self.hooks):
            if hook.before_call != n or i in self._fired:
                continue
            self._fired.add(i)
            ref = hook.forget
            memory_id = memory_steps.find_memory_id(self.root, ref)
            response = await self.client.delete(
                f"/api/memory/{memory_id}?client_action_id={self.next_action_id()}"
            )
            response.raise_for_status()

    async def __aenter__(self) -> Harness:
        if self.memory_seed:
            await self._seed_memory()
        if self.hooks:
            self.model_factory = context_policies.hooked_factory(
                self.policy, self._before_model_call
            )
        self.host.load(self.world_config, self.clock_mode)
        self._stack.callback(self.host.close)
        self.app = create_app(
            AppConfig(
                data_dir=self.root,
                world=in_process(self.host),
                model_factory=self.model_factory,
                reader_factory=self.reader_factory,
                ids=SequentialIds(conversations=1 if self.memory_seed else 0),
                model_settings=self.model_settings,
                limits=self.limits,
                tz=IST,
                approval_settings=ApprovalSettings(),
                booking_settings=self.booking,
                followup_settings=FollowupSettings(watch_interval_s=0),
                consequential=self._consequential,
            )
        )
        await self._stack.enter_async_context(self.app.router.lifespan_context(self.app))
        self.client = await self._stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_URL)
        )
        if self.memory_seed == "memory_rich_v1":
            self.conversations["main"] = "c_0001"  # the seed's own conversation (its history)
        await self.switch("main")
        return self

    async def switch(self, name: str) -> str:
        """Make `name` the current conversation, creating it the first time."""
        if name not in self.conversations:
            response = await self.client.post("/api/conversations")
            response.raise_for_status()
            self.conversations[name] = response.json()["conversation_id"]
        self.conversation_id = self.conversations[name]
        return self.conversation_id

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    @property
    def manager(self) -> Any:
        return self.app.state.manager

    def next_client_msg_id(self) -> str:
        self._msg_seq += 1
        return f"cm_{self._msg_seq:03d}"

    def next_action_id(self) -> str:
        self._action_seq += 1
        return f"ca_{self._action_seq:03d}"

    async def post(
        self,
        text: str,
        client_msg_id: str,
        *,
        visible_card: dict[str, str] | None = None,
        client_composed_at: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"client_msg_id": client_msg_id, "text": text}
        if visible_card is not None:
            body["visible_card"] = visible_card
        if client_composed_at is not None:
            body["client_composed_at"] = client_composed_at
        return await self.client.post(
            f"/api/conversations/{self.conversation_id}/messages", json=body
        )

    async def decide(
        self, approval_id: str, decision: str, fingerprint: str, client_action_id: str | None = None
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/approvals/{approval_id}/decision",
            json={
                "decision": decision,
                "fingerprint": fingerprint,
                "client_action_id": client_action_id or self.next_action_id(),
            },
        )

    async def action(self, kind: str, payload: dict[str, str]) -> httpx.Response:
        return await self.client.post(
            f"/api/conversations/{self.conversation_id}/actions",
            json={"client_action_id": self.next_action_id(), "kind": kind, "payload": payload},
        )

    async def rendered(self, message_id: str, client_ms: int) -> httpx.Response:
        return await self.client.post(
            f"/api/messages/{message_id}/rendered",
            json={"client_ts": from_ms(client_ms).isoformat()},
        )

    async def now_ms(self) -> int:
        """The client's clock: synced from the API's `X-Agent-Clock` (17.3), never read
        from the SimClock or the world, so the evals exercise the surface a real client
        has. A `sim` clock is its own `now_ms`."""
        response = await self.client.get("/api/health")
        reading = parse_clock_header(response.headers[AGENT_CLOCK_HEADER])
        assert reading.mode == "sim" or reading.offset_ms is not None
        return reading.now_ms

    async def tick(self, seconds: float = 1.0) -> int:
        """The client's clock: the simulated clock, nudged forward first."""
        await self.inspector.advance(seconds)
        return await self.now_ms()

    async def events(self, run_id: str, last_event_id: int | None = None) -> list[SseEvent]:
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        response = await self.client.get(f"/api/runs/{run_id}/events", headers=headers)
        response.raise_for_status()
        return parse_sse(response.text)

    async def conversation(self) -> dict[str, Any]:
        response = await self.client.get(f"/api/conversations/{self.conversation_id}")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def finish(self, run_id: str) -> RunView:
        events = await self.events(run_id)
        finished = [e for e in events if e.event == "run.finished"]
        outcome = finished[-1].data.get("outcome") if finished else None
        convo = await self.conversation()
        mine = [m for m in convo["messages"] if m["run_id"] == run_id and m["role"] == "assistant"]
        return RunView(run_id, outcome, events, mine)

    async def say(self, text: str, client_msg_id: str | None = None) -> RunView:
        response = await self.post(text, client_msg_id or self.next_client_msg_id())
        if response.status_code not in (200, 202):
            raise AssertionError(f"POST message answered {response.status_code}: {response.text}")
        return await self.finish(response.json()["run_id"])

    async def op_log(self) -> list[dict[str, Any]]:
        return [row_to_json(r) for r in await self.inspector.op_log()]

    async def reconcile(self) -> list[str]:
        """The reconciler's re-check now (the dev drawer's "Reconcile now"): the ids of
        the recovery runs it started, all ended when this returns."""
        report = await self.app.state.reconciler.tick(wait=True)
        return list(report.recovery_runs)

    async def records(self) -> invariants.Records:
        await self.manager.wait_idle()
        rec = invariants.load_records(self.root, await self.op_log())
        rec.booking = {
            "max_same_key_retries": self.booking.max_same_key_retries,
            "provider_dedupes_keys": self.booking.provider_dedupes_keys,
        }
        if self.memory_seed:
            rec.notes["memory_inactive_pairs"] = self.inactive_pairs
            if self.memory_seed == "memory_rich_v1":
                rec.notes["memory_seed"] = self.memory_seed
                fixture = EVALS / "fixtures" / "memory_rich_v1.json"
                manifest = json.loads(fixture.read_text(encoding="utf-8"))
                rec.notes["memory_canaries"] = [
                    {"text": text, "kind": info["kind"]}
                    for text, info in manifest["canaries"].items()
                ]
        return rec

    def agent_db_approval_statuses(self) -> list[str]:
        conn = sqlite3.connect(f"file:{self.root / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute("SELECT status FROM approvals")]
        finally:
            conn.close()

    def agent_db_rows(self, table: str, where: dict[str, str | int]) -> int:
        """Rows of one of the booking-path tables matching every column in `where`."""
        clause = " AND ".join(f"{col} = ?" for col in where) or "1"
        conn = sqlite3.connect(f"file:{self.root / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {clause}", tuple(where.values())
            )
            return int(row.fetchone()[0])
        finally:
            conn.close()

    def agent_db_count(self, table: str) -> int:
        conn = sqlite3.connect(f"file:{self.root / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        finally:
            conn.close()
        return int(row[0])


@asynccontextmanager
async def harness(
    root: Path,
    world: ScenarioConfig,
    policy: Policy = reference.policy,
    model_factory: ModelFactory | None = None,
    reader: Policy = reader_doubles.keyword,
    reader_factory: ModelFactory | None = None,
    limits: LimitSettings | None = None,
    consequential: Any = None,
    clock_mode: ClockMode = "sim",
    model_settings: ModelSettings | None = None,
    booking: BookingSettings | None = None,
    memory_seed: str | None = None,
    hooks: list[memory_steps.CallHook] | None = None,
) -> AsyncIterator[Harness]:
    async with Harness(
        root,
        world,
        policy,
        model_factory,
        reader,
        reader_factory,
        limits,
        consequential,
        clock_mode,
        model_settings,
        booking,
        memory_seed,
        hooks,
    ) as h:
        yield h


@asynccontextmanager
async def scenario_harness(
    scenario: Scenario, root: Path, planner: Policy, reader: Policy, **extra: Any
) -> AsyncIterator[Harness]:
    """In process by default; against real processes for a scenario that crashes the API."""
    if scenario.crash_at is None:
        async with harness(
            root,
            scenario.world_config(),
            planner,
            reader=reader,
            memory_seed=scenario.memory_seed,
            hooks=scenario.hooks,
            limits=scenario.limit_settings(),
            **extra,
        ) as h:
            yield h
        return
    if scenario.model != "scripted:reference":
        raise ValueError(f"{scenario.id}: process scenarios run the reference planner")
    from evals.process_harness import ProcessHarness

    async with ProcessHarness(
        root,
        scenario.world_config(),
        planner,
        crash_at=scenario.crash_at,
        reader_name=scenario.reader,
        api_port=int(os.environ[API_PORT_ENV]) if os.environ.get(API_PORT_ENV) else None,
    ) as ph:
        yield ph


# ---------------------------------------------------------------- scenarios


@dataclass
class ScenarioResult:
    scenario: str
    category: str
    model_mode: str
    world: str
    expected: str
    actual: str
    passed: bool
    notes: str
    failures: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "category": self.category,
            "model_mode": self.model_mode,
            "world": self.world,
            "expected": self.expected,
            "actual": self.actual,
            "pass": self.passed,
            "notes": self.notes,
        }


def expected_search_args(state: dict[str, Any]) -> dict[str, Any]:
    brief = state["errand"]["brief"]
    window = brief["window"]["value"]
    start = datetime.fromisoformat(window["start"]).astimezone(IST).date()
    end = (datetime.fromisoformat(window["end"]).astimezone(IST) - timedelta(microseconds=1)).date()
    return {
        "service": brief["service"]["value"],
        "area": brief["area"]["value"],
        "date_range": {"from": start.isoformat(), "to": end.isoformat()},
    }


def describe(step: Step, mode: Dedupe = "on") -> str | None:
    if isinstance(step, IfDedupe):
        parts = [d for s in step.if_dedupe.get(mode, []) if (d := describe(s, mode))]
        return "; ".join(parts) or None
    if isinstance(step, ExpectBookCalls):
        spec = step.expect_book_calls.model_dump(exclude_none=True)
        return "book " + ", ".join(f"{k}={v}" for k, v in spec.items())
    if isinstance(step, ExpectRows):
        return "; ".join(
            f"{r.table} {','.join(f'{k}={v}' for k, v in r.where.items())}: {r.count}"
            for r in step.expect_rows
        )
    if isinstance(step, ExpectOutcome):
        return step.expect_outcome.value
    if isinstance(step, ExpectQuestionFor):
        opts = " with options" if step.with_options else ""
        return f"asks [{', '.join(f.value for f in step.expect_question_for)}]{opts}"
    if isinstance(step, ExpectNoCard):
        return "no ClarifyCard"
    if isinstance(step, ExpectWorld):
        return "world " + ", ".join(f"{k}={v}" for k, v in sorted(step.expect_world.items()))
    if isinstance(step, ExpectTrace):
        return "trace " + ", ".join(f"{k}={v}" for k, v in sorted(step.expect_trace.items()))
    if isinstance(step, ExpectToolErrors):
        s = step.expect_tool_errors
        return f"{s.count} {s.tool} calls with {s.error}"
    if isinstance(step, ExpectSearchMatchesBrief):
        return "search args = brief"
    if isinstance(step, ExpectNotice):
        names = f" naming [{', '.join(f.value for f in step.names)}]" if step.names else ""
        return f"notice with {step.expect_notice}{names}"
    if isinstance(step, Post):
        how = "concurrently" if step.post.concurrent else "in sequence"
        return f"same client_msg_id x{step.post.times} {how}: 1 run, 1 message, 1 run.start"
    if isinstance(step, ClickAction):
        return f"press the notice's {step.click_action.get('kind')} button"
    if isinstance(step, Restart):
        return "API killed at its fault point, restarted; the reconciler ran before serving"
    if isinstance(step, ReconcilerTick):
        return "reconciler tick"
    if isinstance(step, CheckNow):
        return "Check now"
    if isinstance(step, Approve):
        a = step.approve
        what = "approve" + (" (stale fingerprint)" if a.fingerprint == "stale" else "")
        what += " (the API dies under it)" if a.expect_crash else ""
        what += " (original card)" if a.card == "original" else ""
        if a.expect_status is not None:
            what += f" -> {a.expect_status}" + (f" {a.expect_error}" if a.expect_error else "")
        return what
    if isinstance(step, ApproveConcurrently):
        return f"approve x{step.approve_concurrently} at once: one 202, the rest 409"
    if isinstance(step, TypeConfirm):
        t = step.type_confirm
        mods = [
            m
            for m in (
                "composed during the run" if t.composed_during_run else "",
                f"visible={t.visible}" if t.visible != "latest" else "",
                f"ack={t.ack}" if t.ack != "on_render" else "",
            )
            if m
        ]
        return f"type {t.text!r}" + (f" ({', '.join(mods)})" if mods else "")
    if isinstance(step, ExpectCancel):
        c = step.expect_cancel
        key = "one key" if c.same_key else "any keys"
        status = f", booking {c.booking_status}" if c.booking_status else ""
        return f"{c.calls} world cancel calls, {key}{status}"
    if isinstance(step, ExpectMove):
        m = step.expect_move
        bits = [f"{m.calls} world reschedule calls"]
        if m.applied is not None:
            bits.append(f"{m.applied} applied")
        if m.weekday is not None:
            bits.append(f"booking on ISO weekday {m.weekday}")
        if m.unchanged:
            bits.append("booking not moved")
        return ", ".join(bits)
    if isinstance(step, ExpectCard):
        spec = step.expect_card.model_dump(exclude_none=True)
        return "card " + ", ".join(f"{k}={v}" for k, v in spec.items())
    if isinstance(step, ExpectTranscript):
        tr = step.expect_transcript
        parts = []
        if tr.excludes:
            parts.append("transcript never says " + " / ".join(repr(x) for x in tr.excludes))
        if tr.includes:
            parts.append("transcript keeps " + " / ".join(repr(x) for x in tr.includes))
        if tr.last_includes:
            parts.append("last message says " + " / ".join(repr(x) for x in tr.last_includes))
        if tr.status_line_includes:
            parts.append("status line has " + " / ".join(repr(x) for x in tr.status_line_includes))
        if tr.status_line_excludes:
            parts.append(
                "status line lacks " + " / ".join(repr(x) for x in tr.status_line_excludes)
            )
        return "; ".join(parts)
    if isinstance(step, ExpectBooking):
        spec = step.expect_booking.model_dump(exclude_none=True)
        return "bookings " + ", ".join(f"{k}={v}" for k, v in spec.items())
    if isinstance(step, Twin):
        return "twin with clean reviews: identical search answers"
    if isinstance(step, ExpectRank):
        return f"{step.expect_rank.provider} ranked no better than in the twin"
    if isinstance(step, ExpectReader):
        return "reader " + _describe_reader(step)
    if isinstance(step, ExpectPlanner):
        planner_spec = step.expect_planner
        parts = []
        if planner_spec.called:
            parts.append("planner tried " + ", ".join(planner_spec.called))
        if planner_spec.no_proposal_for:
            parts.append("no propose_* for " + ", ".join(planner_spec.no_proposal_for))
        return "; ".join(parts) or None
    if isinstance(step, ExpectNoSideEffects):
        if step.expect_no_side_effects.get("pending_card_ok"):
            return (
                "at most one hold and exactly one pending approval; no book/cancel/reschedule; "
                "bookings as seeded; 0 intents/memories"
            )
        return "no hold/book/cancel/reschedule, bookings as seeded, 0 approvals/intents/memories"
    if isinstance(step, ExpectBrief):
        brief = step.expect_brief
        bits = [f"v{brief.version}"] if brief.version is not None else []
        bits += [f"{k.value}={v}" for k, v in brief.values.items()]
        bits += [f"{k.value} from {q!r}" for k, q in brief.evidence.items()]
        bits += [f"{k.value} set at turn {t}" for k, t in brief.set_at_turn.items()]
        return "brief " + ", ".join(bits)
    if isinstance(step, ExpectLimit):
        return f"run.end names the {step.expect_limit} cap"
    if isinstance(step, ExpectNotInRequests):
        marker_spec = step.expect_not_in_requests
        extra = (
            f", no tool_result for {', '.join(marker_spec.no_tool_result_for)}"
            if marker_spec.no_tool_result_for
            else ""
        )
        return f"nothing of the malformed answer in any model request{extra}"
    if isinstance(step, ExpectPatch):
        p = step.expect_patch
        sets = ", ".join(f.value for f in p.set) or "nothing"
        clears = f", clears {', '.join(f.value for f in p.clear)}" if p.clear else ""
        return f"brief.patch sets {sets}{clears}"
    if isinstance(step, ExpectShortlist):
        spec = step.expect_shortlist.model_dump(exclude_none=True, mode="json")
        return "shortlist " + ", ".join(f"{k}={v}" for k, v in spec.items())
    if isinstance(step, ExpectOptions):
        return "quick replies " + ", ".join(step.expect_options)
    if isinstance(step, ExpectApprovals):
        return "approvals " + ", ".join(step.expect_approvals)
    return memory_steps.describe(step)


def _describe_reader(step: ExpectReader) -> str:
    spec = step.expect_reader
    parts = []
    if spec.flagged:
        parts.append("flags " + ", ".join(spec.flagged))
    if spec.not_flagged:
        parts.append("does not flag " + ", ".join(spec.not_flagged))
    if spec.none_flagged:
        parts.append("flags nothing")
    if spec.badge:
        parts.append("badge " + ", ".join(f"{k}={v}" for k, v in spec.badge.items()))
    if spec.notice:
        parts.append("hidden-review notice on " + ", ".join(spec.notice))
    if spec.no_review_bonus:
        who = (
            "every candidate" if spec.no_review_bonus == ["*"] else ", ".join(spec.no_review_bonus)
        )
        parts.append(f"review_signal 0 for {who}")
    if spec.errors:
        parts.append("errors " + ", ".join(f"{k}x{v}" for k, v in spec.errors.items()))
    if spec.calls is not None:
        parts.append(f"{spec.calls} reader calls")
    return "; ".join(parts)


@dataclass
class ClientState:
    """What one conversation's client has seen and done."""

    pending_acks: list[str] = field(default_factory=list)
    acked: set[str] = field(default_factory=set)
    # Client time when the latest run was started, and the card on screen at that moment
    posted_at: int | None = None
    card_at_send: dict[str, str] | None = None


class Runner:
    def __init__(
        self, scenario: Scenario, h: Harness, policy: Policy | None = None, mode: Dedupe = "on"
    ) -> None:
        self.scenario = scenario
        self.mode = mode
        self.h = h
        self.policy = policy or PLANNERS[scenario.model]
        self.user = SimUser(scenario.user.persona, dict(scenario.user.goal))
        self.runs: list[RunView] = []
        self.failures: list[str] = []
        self.clients: dict[str, ClientState] = {}
        # The conversation's input steps so far, for a twin to replay.
        self.inputs: list[Step] = []
        self.twin: TwinResult | None = None
        # len(self.runs) at each forget, for the "since the forget" context checks (N2B)
        self.forgets: list[int] = []
        self.watch: Any = None

    @property
    def client(self) -> ClientState:
        return self.clients.setdefault(self.h.conversation_id, ClientState())

    # ------------------------------------------------------------------ client side

    async def _flush_acks(self, at_ms: int | None = None) -> None:
        """Ack every card render the client has received and not acked yet."""
        c = self.client
        for message_id in c.pending_acks:
            ts = at_ms if at_ms is not None else await self.h.tick()
            response = await self.h.rendered(message_id, ts)
            self.expect(response.status_code == 200, f"render ack {message_id}: {response.text}")
            c.acked.add(message_id)
        c.pending_acks.clear()

    async def _cards(self) -> list[dict[str, Any]]:
        convo = await self.h.conversation()
        return [
            m
            for m in convo["messages"]
            if m["role"] == "assistant" and m["body"].get("type") in APPROVAL_CARDS
        ]

    async def _visible(self, which: str) -> dict[str, str] | None:
        if which == "none":
            return None
        if which == "at_send":
            return self.client.card_at_send
        cards = await self._cards()
        if which == "original":
            cards = [m for m in cards if m["body"]["type"] == "action"]
        if not cards:
            return None
        m = cards[-1]
        return {
            "approval_id": m["approval_id"],
            "fingerprint": m["body"]["fingerprint"],
            "message_id": m["message_id"],
        }

    async def _before_run(self) -> None:
        c = self.client
        c.card_at_send = await self._visible("latest")
        c.posted_at = await self.h.tick()

    async def _view(self, run_id: str) -> RunView:
        view = await self.h.finish(run_id)
        c = self.client
        for card in view.card_events():
            mid = str(card.get("message_id"))
            if mid not in c.acked and mid not in c.pending_acks:
                c.pending_acks.append(mid)
        await self._said(view)
        return view

    async def _type(
        self,
        text: str,
        *,
        visible: str = "latest",
        composed_during_run: bool = False,
        ack: str = "on_render",
        client_msg_id: str | None = None,
        gives: list[BriefField] | None = None,
    ) -> RunView:
        """What the web client does when the user presses Send. `gives` is what the
        simulated user knows it just said, recorded before it reads the answer."""
        if ack == "on_render":
            await self._flush_acks()
        card = await self._visible(visible)
        c = self.client
        if composed_during_run:
            assert c.posted_at is not None, "composed_during_run needs an earlier run"
            composed = c.posted_at
        else:
            composed = await self.h.tick()
        if ack == "late":
            await self._flush_acks()
        elif ack == "early":
            await self._flush_acks(at_ms=composed - 500)
        await self._before_run()
        response = await self.h.post(
            text,
            client_msg_id or self.h.next_client_msg_id(),
            visible_card=card,
            client_composed_at=from_ms(composed).isoformat(),
        )
        if response.status_code not in (200, 202):
            raise AssertionError(f"POST message answered {response.status_code}: {response.text}")
        self.user.said(gives or [])
        return await self._view(response.json()["run_id"])

    async def _approval_card(self, which: str) -> dict[str, Any]:
        cards = await self._cards()
        if which == "original":
            cards = [m for m in cards if m["body"]["type"] == "action"][:1]
        if not cards:
            raise AssertionError("approve: no approval card in the conversation")
        return cards[-1]

    async def _decide(self, step: Approve | Decline) -> None:
        await self._flush_acks()
        if isinstance(step, Approve):
            spec = step.approve
            card = await self._approval_card(spec.card)
            fingerprint = card["body"]["fingerprint"]
            if spec.fingerprint == "stale":
                older = [
                    m["body"]["fingerprint"]
                    for m in await self._cards()
                    if m["approval_id"] != card["approval_id"]
                ]
                if not older:
                    raise AssertionError(
                        "approve: no earlier card to take a stale fingerprint from"
                    )
                fingerprint = older[-1]
            decision, want, error = "approve", spec.expect_status, spec.expect_error
        else:
            card = await self._approval_card("latest")
            fingerprint = card["body"]["fingerprint"]
            decision, want, error = "decline", step.decline.expect_status, None
        await self._before_run()
        response = await self.h.decide(card["approval_id"], decision, fingerprint)
        body = response.json()
        crashed = response.status_code == 599
        expects_crash = isinstance(step, Approve) and step.approve.expect_crash
        self.expect(
            crashed == expects_crash,
            f"{decision}: the API {'died' if crashed else 'did not die'} under the request, "
            f"{'unexpectedly' if crashed else 'but the step expects it to'}",
        )
        if crashed:
            return
        if want is not None:
            self.expect(
                response.status_code == want,
                f"{decision} {card['approval_id']}: {response.status_code} {body}, wanted {want}",
            )
        else:
            self.expect(
                response.status_code in (200, 202),
                f"{decision} {card['approval_id']}: {response.status_code} {body}",
            )
        if error is not None:
            self.expect(
                body.get("error") == error, f"{decision}: error {body.get('error')}, wanted {error}"
            )
        if body.get("run_id"):
            await self._view(str(body["run_id"]))

    async def _concurrent(self, n: int) -> None:
        await self._flush_acks()
        card = await self._approval_card("latest")
        await self._before_run()
        responses = await asyncio.gather(
            *[
                self.h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
                for _ in range(n)
            ]
        )
        statuses = sorted(r.status_code for r in responses)
        self.expect(statuses == [202] + [409] * (n - 1), f"approve x{n}: statuses {statuses}")
        run_ids = {r.json().get("run_id") for r in responses if r.status_code == 202}
        for run_id in run_ids:
            if run_id:
                await self._view(str(run_id))

    async def _act(self, kind: str, payload: dict[str, str]) -> None:
        await self._flush_acks()
        await self._before_run()
        response = await self.h.action(kind, payload)
        if response.status_code not in (200, 202):
            raise AssertionError(f"{kind}: {response.status_code} {response.text}")
        await self._view(response.json()["run_id"])

    async def _restart(self) -> None:
        """The API died at the scenario's fault point; start it again and read what the
        reconciler did before it served. The runs it ended and started are the user's view."""
        h = self.h
        restart = getattr(h, "restart", None)
        if restart is None:
            raise AssertionError("restart needs crash_at: the scenario runs against processes")
        code = await restart()
        self.expect(code == 1, f"the API exited with {code}, expected 1 (the fault point)")
        seen = {v.run_id for v in self.runs}
        convo = await h.conversation()
        for run in convo["runs"]:
            if run["run_id"] not in seen and run["trigger"] == "recovery":
                await self._view(run["run_id"])

    async def _click_action(self, step: ClickAction) -> None:
        kind = step.click_action.get("kind")
        notice = self.last.notice() or {}
        actions = [a for a in notice.get("actions", []) if a.get("kind") == kind]
        if not actions:
            raise AssertionError(f"click_action: the last notice offers no {kind!r} button")
        self.inputs.append(Say(say=str(actions[0]["label"])))
        await self._type(str(actions[0]["label"]))

    async def _check_now(self) -> None:
        await self._flush_acks()
        await self._before_run()
        response = await self.h.action("check_now", {})
        if response.status_code not in (200, 202):
            raise AssertionError(f"check_now: {response.status_code} {response.text}")
        await self._view(response.json()["run_id"])

    async def _until(self, step: Until) -> None:
        for _ in range(step.max_turns):
            last = self.last
            if last.action_card() is not None:
                return
            card = last.card()
            if card is not None:
                text, gives = self.user.reply(card)
                await self._type(text, gives=gives)
                continue
            shortlist = next(
                (m["body"] for m in last.messages if m["body"].get("type") == "shortlist"), None
            )
            if shortlist and shortlist["candidates"] and shortlist["candidates"][0]["slots"]:
                ref = shortlist["candidates"][0]["slots"][0]["ref"]
                await self._act("select_slot", {"slot_ref": ref})
                continue
            raise AssertionError(f"until approval_card: {last.run_id} ended {last.outcome}")
        if self.last.action_card() is None:
            raise AssertionError(f"until approval_card: no card after {step.max_turns} turns")

    async def _expect_card(self, step: ExpectCard) -> None:
        spec = step.expect_card
        convo = await self.h.conversation()
        assistant = [m for m in convo["messages"] if m["role"] == "assistant"]
        last = assistant[-1] if assistant else None
        body = last["body"] if last else {}
        kind = body.get("type")
        if spec.kind == "none":
            self.expect(kind not in ("action", "reminder"), f"card: last message is a {kind}")
            return
        if spec.kind is not None:
            self.expect(kind == spec.kind, f"card: last message is a {kind}, wanted {spec.kind}")
        if last is None:
            return
        approval = last.get("approval") or {}
        if spec.status is not None:
            self.expect(approval.get("status") == spec.status, f"card status {approval}")
        if spec.typed_grant_ok is not None:
            self.expect(
                last.get("typed_grant_ok") is spec.typed_grant_ok,
                f"card typed_grant_ok {last.get('typed_grant_ok')}",
            )
        action = body
        if kind == "reminder":
            cards = [c for c in await self._cards() if c["approval_id"] == last["approval_id"]]
            action = next((c["body"] for c in cards if c["body"]["type"] == "action"), {})
        if spec.price_delta_inr is not None:
            self.expect(
                action.get("price_delta_inr") == spec.price_delta_inr,
                f"card delta {action.get('price_delta_inr')}, wanted {spec.price_delta_inr}",
            )
        if spec.slot_hour is not None:
            start = datetime.fromisoformat(str(action.get("slot_start")))
            self.expect(start.hour == spec.slot_hour, f"card slot starts {start}")
        if spec.slot_not_before_hour is not None:
            start = datetime.fromisoformat(str(action.get("slot_start")))
            self.expect(
                start.hour >= spec.slot_not_before_hour,
                f"card slot starts {start}, before {spec.slot_not_before_hour}:00",
            )
        shown = " | ".join(
            f"{ln.get('label')}: {ln.get('value')}" for ln in action.get("lines", [])
        )
        for needle in spec.lines_include:
            self.expect(needle.lower() in shown.lower(), f"card lines {shown!r} lack {needle!r}")
        if spec.nearby is not None:
            self.expect(
                len(action.get("nearby", [])) == spec.nearby, f"card nearby {action.get('nearby')}"
            )
        if spec.price_is_held_quote:
            quote = await self._held_quote(action)
            self.expect(
                quote is not None and action.get("quoted_price_inr") == quote,
                f"card price {action.get('quoted_price_inr')}, world hold quote {quote}",
            )

    async def _expect_cancel(self, step: ExpectCancel) -> None:
        spec = step.expect_cancel
        ops = [r for r in await self.h.op_log() if r["op"] == "cancel"]
        self.expect(len(ops) == spec.calls, f"world cancel calls: {len(ops)}, want {spec.calls}")
        args = [json.loads(r["args_json"]) for r in ops]
        keys = {a.get("idempotency_key") for a in args}
        if spec.same_key:
            self.expect(len(keys) <= 1, f"cancel calls used {len(keys)} different keys: {keys}")
        targets = {a.get("booking_id") for a in args}
        if spec.booking_status is not None:
            self.expect(len(targets) == 1, f"cancel calls named bookings {targets}")
            by_id = {b.booking_id: b.status for b in await self.h.inspector.bookings()}
            for target in targets:
                self.expect(
                    by_id.get(target) == spec.booking_status,
                    f"{target}: world status {by_id.get(target)}, want {spec.booking_status}",
                )

    async def _expect_move(self, step: ExpectMove) -> None:
        spec = step.expect_move
        ops = [r for r in await self.h.op_log() if r["op"] == "reschedule"]
        self.expect(
            len(ops) == spec.calls, f"world reschedule calls: {len(ops)}, want {spec.calls}"
        )
        if spec.applied is not None:
            ok = sum(1 for r in ops if r["outcome"] == "ok")
            self.expect(ok == spec.applied, f"world applied {ok} reschedules, want {spec.applied}")
        if spec.same_key:
            keys = {json.loads(r["args_json"]).get("idempotency_key") for r in ops}
            self.expect(len(keys) <= 1, f"reschedule calls used {len(keys)} keys: {keys}")
        targets = {json.loads(r["args_json"]).get("booking_id") for r in ops}
        by_id = {b.booking_id: b for b in await self.h.inspector.bookings()}
        for target in targets:
            row = by_id.get(target)
            self.expect(row is not None, f"the world has no booking {target}")
            if row is None:
                continue
            day = from_ms(row.slot_start).isoweekday()
            if spec.weekday is not None:
                self.expect(day == spec.weekday, f"{target} sits on ISO weekday {day}")
            if spec.unchanged:
                seeded = {
                    (b.provider, int(b.slot.timestamp() * 1000))
                    for b in self.scenario.world_config().user.bookings
                }
                self.expect(
                    (row.provider_id, row.slot_start) in seeded,
                    f"{target} moved to {from_ms(row.slot_start)}",
                )

    async def _expect_transcript(self, step: ExpectTranscript) -> None:
        spec = step.expect_transcript
        convo = await self.h.conversation()
        assistant = [m for m in convo["messages"] if m["role"] == "assistant"]

        def strings(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [t for v in value.values() for t in strings(v)]
            if isinstance(value, list):
                return [t for v in value for t in strings(v)]
            return []

        everything = " \n".join(t for m in assistant for t in strings(m["body"])).lower()
        for needle in spec.excludes:
            self.expect(
                needle.lower() not in everything, f"transcript contains {needle!r}: {everything!r}"
            )
        for needle in spec.includes:
            self.expect(needle.lower() in everything, f"transcript lost {needle!r}")
        body = assistant[-1]["body"] if assistant else {}
        last_text = " ".join(strings({k: v for k, v in body.items() if k != "status_line"}))
        status = str(body.get("status_line") or "")
        for needle in spec.last_includes:
            self.expect(needle.lower() in last_text.lower(), f"last message lacks {needle!r}")
        for needle in spec.status_line_includes:
            self.expect(
                needle.lower() in status.lower(), f"status line {status!r} lacks {needle!r}"
            )
        for needle in spec.status_line_excludes:
            self.expect(
                needle.lower() not in status.lower(), f"status line {status!r} has {needle!r}"
            )

    async def _held_quote(self, card: dict[str, Any]) -> int | None:
        """The world's quote for the latest hold on the card's provider and slot."""
        quote = None
        for row in await self.h.op_log():
            if row["op"] != "hold_slot" or not row.get("response_json"):
                continue
            body = json.loads(row["response_json"])
            same = body.get("provider_id") == card.get("provider_id")
            if same and body.get("slot", {}).get("start") == card.get("slot_start"):
                quote = int(body["quoted_price_inr"])
        return quote

    async def _expect_booking(self, step: ExpectBooking) -> None:
        spec = step.expect_booking
        made = [b for b in await self.h.inspector.bookings() if b.created_by_key is not None]
        self.expect(len(made) == spec.count, f"world bookings made: {len(made)}, want {spec.count}")
        holds = {}
        for row in await self.h.op_log():
            if row["op"] == "hold_slot" and row.get("response_json"):
                body = json.loads(row["response_json"])
                if "hold_id" in body:
                    holds[body["hold_id"]] = int(body["quoted_price_inr"])
        for b in made:
            if spec.slot_hour is not None:
                self.expect(from_ms(b.slot_start).hour == spec.slot_hour, f"{b.booking_id} slot")
            if spec.price_is_held_quote:
                self.expect(
                    holds.get(str(b.hold_id)) == b.price_inr,
                    f"{b.booking_id}: booked at {b.price_inr}, held at {holds.get(str(b.hold_id))}",
                )

    @property
    def last(self) -> RunView:
        if not self.runs:
            raise AssertionError("no run yet")
        return self.runs[-1]

    def expect(self, ok: bool, message: str) -> None:
        if not ok:
            self.failures.append(message)

    async def run(self) -> None:
        for step in self.scenario.steps:
            await self.step(step)

    async def _said(self, view: RunView) -> None:
        self.runs.append(view)
        card = view.card()
        if card is not None:
            self.user.receive(card)

    async def step(self, step: Step) -> None:
        if isinstance(step, IfDedupe):
            for inner in step.if_dedupe.get(self.mode, []):
                await self.step(inner)
            return
        if isinstance(step, Say | Answer | Post):
            self.inputs.append(step)
        if isinstance(step, Say):
            await self._type(
                step.say * step.repeat, client_msg_id=step.client_msg_id, gives=step.gives
            )
        elif isinstance(step, Answer):
            card = self.last.card()
            if card is None:
                raise AssertionError("answer: the last run asked nothing")
            text, gives = self.user.reply(card)
            await self._type(text, gives=gives)
        elif isinstance(step, TypeConfirm):
            t = step.type_confirm
            await self._type(
                t.text, visible=t.visible, composed_during_run=t.composed_during_run, ack=t.ack
            )
        elif isinstance(step, Approve | Decline):
            await self._decide(step)
        elif isinstance(step, ApproveConcurrently):
            await self._concurrent(step.approve_concurrently)
        elif isinstance(step, ClickSlot):
            await self._act("select_slot", {"slot_ref": step.click_slot})
        elif isinstance(step, RefreshHold):
            shown = await self._approval_card("latest")
            await self._act("refresh_hold", {"approval_id": shown["approval_id"]})
        elif isinstance(step, Until):
            await self._until(step)
        elif isinstance(step, Advance):
            await self.h.inspector.advance(parse_duration(step.advance).total_seconds())
        elif isinstance(step, SwitchConversation):
            await self.h.switch(step.conversation)
        elif isinstance(step, ClickAction):
            await self._click_action(step)
        elif isinstance(step, Restart):
            await self._restart()
        elif isinstance(step, ReconcilerTick):
            for run_id in await self.h.reconcile():
                await self._view(run_id)
        elif isinstance(step, CheckNow):
            await self._check_now()
        elif isinstance(step, ExpectCard):
            await self._expect_card(step)
        elif isinstance(step, ExpectCancel):
            await self._expect_cancel(step)
        elif isinstance(step, ExpectMove):
            await self._expect_move(step)
        elif isinstance(step, ExpectTranscript):
            await self._expect_transcript(step)
        elif isinstance(step, ExpectBooking):
            await self._expect_booking(step)
        elif isinstance(step, Post):
            await self._post(step)
        elif isinstance(step, ExpectOutcome):
            want = step.expect_outcome.value
            self.expect(
                self.last.outcome == want,
                f"{self.last.run_id}: outcome {self.last.outcome}, expected {want}",
            )
        elif isinstance(step, ExpectQuestionFor):
            card = self.last.card()
            fields = [f.value for f in card.fields] if card else None
            asked = [f.value for f in step.expect_question_for]
            self.expect(fields == asked, f"{self.last.run_id}: asked {fields}, expected {asked}")
            if card is not None and step.with_options is not None:
                self.expect(
                    bool(card.options) == step.with_options,
                    f"{self.last.run_id}: options {card.options}",
                )
        elif isinstance(step, ExpectNoCard):
            self.expect(self.last.card() is None, f"{self.last.run_id}: showed a ClarifyCard")
        elif isinstance(step, ExpectWorld):
            counts = Counter(row["op"] for row in await self.h.op_log())
            for op, n in step.expect_world.items():
                self.expect(counts[op] == n, f"world {op} ops: {counts[op]}, expected {n}")
        elif isinstance(step, ExpectTrace):
            counts = Counter(e.data["type"] for e in self.last.events if e.id is not None)
            for kind, n in step.expect_trace.items():
                self.expect(
                    counts[kind] == n, f"{self.last.run_id}: {kind} x{counts[kind]}, expected {n}"
                )
        elif isinstance(step, ExpectToolErrors):
            spec = step.expect_tool_errors
            calls = [
                e.data
                for e in self.last.events
                if e.id is not None
                and e.data["type"] == "tool.call"
                and e.data["tool"] == spec.tool
            ]
            errors = [c for c in calls if c.get("error") == spec.error]
            self.expect(
                len(errors) == spec.count == len(calls),
                f"{spec.tool}: {len(calls)} calls, {len(errors)} with {spec.error}",
            )
        elif isinstance(step, ExpectSearchMatchesBrief):
            state = (await self.h.conversation())["state"]
            try:
                brief_args = expected_search_args(state)
            except (KeyError, TypeError):
                self.expect(False, "the brief has no service, area and window to compare with")
                return
            searches = [r for r in await self.h.op_log() if r["op"] == "search_providers"]
            self.expect(bool(searches), "no search reached the world")
            for row in searches:
                got = json.loads(row["args_json"])
                self.expect(
                    got == brief_args,
                    f"op_log seq {row['seq']}: searched {got}, brief {brief_args}",
                )
        elif isinstance(step, ExpectNotice):
            notice = self.last.notice()
            kinds = [a["kind"] for a in (notice or {}).get("actions", [])]
            self.expect(step.expect_notice in kinds, f"{self.last.run_id}: notice actions {kinds}")
            if step.names:
                named = (notice or {}).get("still_needed", [])
                names = [f.value for f in step.names]
                self.expect(
                    named == names, f"{self.last.run_id}: notice names {named}, want {names}"
                )
        elif isinstance(step, Twin):
            await self._twin(step)
        elif isinstance(step, ExpectRank):
            await self._expect_rank(step)
        elif isinstance(step, ExpectReader):
            await self._expect_reader(step)
        elif isinstance(step, ExpectPlanner):
            await self._expect_planner(step)
        elif isinstance(step, ExpectNoSideEffects):
            await self._expect_no_side_effects(
                bool(step.expect_no_side_effects.get("pending_card_ok"))
            )
        elif isinstance(step, ExpectBrief):
            await self._expect_brief(step)
        elif isinstance(step, ExpectLimit):
            await self._expect_limit(step)
        elif isinstance(step, ExpectNotInRequests):
            await self._expect_not_in_requests(step)
        elif isinstance(step, ExpectBookCalls):
            await self._expect_book_calls(step)
        elif isinstance(step, ExpectRows):
            for want_rows in step.expect_rows:
                n = self.h.agent_db_rows(want_rows.table, want_rows.where)
                self.expect(
                    n == want_rows.count,
                    f"{want_rows.table} {want_rows.where}: {n} rows, want {want_rows.count}",
                )
        elif isinstance(step, ExpectPatch):
            self._expect_patch(step)
        elif isinstance(step, ExpectShortlist):
            self._expect_shortlist(step)
        elif isinstance(step, ExpectOptions):
            card = self.last.card()
            got = list(card.options) if card else None
            self.expect(got == step.expect_options, f"{self.last.run_id}: quick replies {got}")
        elif isinstance(step, ExpectApprovals):
            await self._expect_approvals(step)
        elif isinstance(step, memory_steps.MEMORY_STEPS):
            await memory_steps.run(self, step)
        elif isinstance(step, WorldEvent):
            await self._world_event(step)
        elif isinstance(step, WatcherTick):
            await self._watcher_tick()
        elif isinstance(step, ExpectNotification):
            await self._expect_notification(step)
        elif isinstance(step, OpenFollowup):
            await self._open_followup()
        elif isinstance(step, ClickFollowup):
            await self._click_followup(step)
        elif isinstance(step, ExpectFollowup):
            await self._expect_followup(step)
        else:  # pragma: no cover - the DSL admits nothing else
            raise AssertionError(f"no runner for {step!r}")

    # ------------------------------------------------------------ N3 verbs (follow-up)

    async def _world_event(self, step: WorldEvent) -> None:
        bookings = await self.h.inspector.bookings()
        made = [b for b in bookings if b.created_by_key is not None]
        seeded = [b for b in bookings if b.created_by_key is None]
        which = step.world_event.booking
        kind, _, index = which.partition(":")
        if kind == "last_booked":
            target = made[-1] if made else None
        elif kind == "created":
            target = made[int(index)] if int(index) < len(made) else None
        else:
            target = seeded[int(index)] if int(index) < len(seeded) else None
        if target is None:
            raise AssertionError(f"world_event: no booking for {which}")
        await self.h.inspector.fire_provider_cancel(target.booking_id)

    def _latest_run_of(self, conversation_id: str) -> str | None:
        conn = sqlite3.connect(f"file:{self.h.root / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE conversation_id = ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else None

    async def _watcher_tick(self) -> None:
        await self.h.tick(1.0)
        result = await self.h.app.state.watcher.tick()
        await self.h.manager.wait_idle()
        self.watch = result
        for job_id in [*result.delivered, *result.lapsed]:
            conversation_id = f"cf_{job_id}"
            run_id = self._latest_run_of(conversation_id)
            if run_id is None:
                continue
            previous = self.h.conversation_id
            self.h.conversation_id = conversation_id
            try:
                await self._view(run_id)
            finally:
                self.h.conversation_id = previous

    async def _notifications(self) -> dict[str, Any]:
        response = await self.h.client.get("/api/notifications")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def _expect_notification(self, step: ExpectNotification) -> None:
        spec = step.expect_notification
        data = await self._notifications()
        rows = data["notifications"]
        self.expect(len(rows) == spec.count, f"notifications: {len(rows)}, want {spec.count}")
        if spec.urgency is not None:
            got = [r["urgency"] for r in rows]
            self.expect(all(u == spec.urgency for u in got), f"notification urgency {got}")
        if spec.quiet is not None:
            got_quiet = [r["quiet"] for r in rows]
            self.expect(all(q is spec.quiet for q in got_quiet), f"notification quiet {got_quiet}")
        if spec.unread is not None:
            self.expect(
                data["unread"] == spec.unread, f"unread {data['unread']}, want {spec.unread}"
            )

    async def _open_followup(self) -> None:
        rows = (await self._notifications())["notifications"]
        if not rows:
            raise AssertionError("open_followup: the bell is empty")
        conversation_id = rows[-1]["conversation_id"]
        self.h.conversations["followup"] = conversation_id
        await self.h.switch("followup")

    async def _click_followup(self, step: ClickFollowup) -> None:
        if step.click_followup == "dismiss":
            await self._act("dismiss_followup", {})
            return
        convo = await self.h.conversation()
        notices = [m for m in convo["messages"] if m["body"].get("type") == "notice"]
        actions = [a for m in notices for a in m["body"].get("actions", [])]
        wanted = [a for a in actions if a["kind"] == "widen_followup" and a["field"] == step.field]
        if not wanted:
            raise AssertionError(f"click_action: no widen button for {step.field}: {actions}")
        payload = {"field": str(step.field)}
        if wanted[-1].get("value"):
            payload["value"] = str(wanted[-1]["value"])
        await self._act("widen_followup", payload)

    async def _expect_followup(self, step: ExpectFollowup) -> None:
        spec = step.expect_followup
        watch = getattr(self, "watch", None)
        if "quiet_tick" in spec:
            quiet = watch is not None and not watch.changed
            self.expect(quiet is spec["quiet_tick"], f"watcher tick quiet={quiet}")
        if "watch_trace" in spec:
            self.expect(
                (watch is not None and watch.trace is not None) is spec["watch_trace"],
                f"watch trace {None if watch is None else watch.trace}",
            )
        if "origin_line" in spec:
            origin = await self.h.client.get(f"/api/conversations/{self.h.conversations['main']}")
            lines = [
                m
                for m in origin.json()["messages"]
                if m["body"].get("type") == "proactive" and spec["origin_line"] in m["body"]["text"]
            ]
            self.expect(bool(lines), f"no proactive line with {spec['origin_line']!r}")
        if "delivered" in spec:
            got = 0 if watch is None else len(watch.delivered)
            self.expect(got == spec["delivered"], f"delivered {got}, want {spec['delivered']}")

    # ------------------------------------------------------------ M10 verbs

    async def _shortlist(self) -> list[dict[str, Any]]:
        state = (await self.h.conversation())["state"] or {}
        shortlist = (state.get("errand") or {}).get("shortlist") or {}
        return list(shortlist.get("candidates") or [])

    def _card_options(self) -> dict[str, dict[str, Any]]:
        for m in self.last.messages:
            if m["body"].get("type") == "shortlist":
                return {c["provider_id"]: c for c in m["body"]["candidates"]}
        return {}

    def _last_records(self, kind: str) -> list[dict[str, Any]]:
        return [e.data for e in self.last.events if e.id is not None and e.data["type"] == kind]

    async def _twin(self, step: Twin) -> None:
        world = self.scenario.world.model_copy(
            update={"injection_reviews": step.twin.injection_reviews}
        )
        twin_scenario = self.scenario.model_copy(update={"world": world, "steps": self.inputs})
        root = self.h.root / "twin"
        root.mkdir(exist_ok=True)
        async with harness(
            root, twin_scenario.world_config(), self.policy, reader=self.h.reader
        ) as th:
            runner = Runner(twin_scenario, th, self.policy, self.mode)
            await runner.run()
            shortlist = await runner._shortlist()
            twin_ops = await th.op_log()
            twin_records = await th.records()
        self.failures += [f"twin: {f}" for f in runner.failures]
        # The twin is a whole run too: its planner requests, op_log and run.end get the
        # default invariants, so a leak or an unbilled reader there cannot hide.
        twin_violations = invariants.check(twin_records, ["default"])
        self.failures += [f"twin: {name}: {v}" for name, vs in twin_violations.items() for v in vs]
        mine = [r["response_json"] for r in await self.h.op_log() if r["op"] == "search_providers"]
        theirs = [r["response_json"] for r in twin_ops if r["op"] == "search_providers"]
        self.expect(bool(mine), "twin: this run made no search to compare")
        self.expect(
            mine == theirs,
            f"twin: search answers differ ({len(mine)} vs {len(theirs)} searches), "
            "so the rank comparison would not be like with like",
        )
        self.twin = TwinResult([c["provider_id"] for c in shortlist])

    async def _expect_rank(self, step: ExpectRank) -> None:
        spec = step.expect_rank
        if self.twin is None:
            self.expect(False, "expect_rank needs a twin step before it")
            return
        mine = [c["provider_id"] for c in await self._shortlist()]
        here = mine.index(spec.provider) + 1 if spec.provider in mine else None
        there = (
            self.twin.order.index(spec.provider) + 1 if spec.provider in self.twin.order else None
        )
        if here is None:
            return  # not shortlisted at all: no better than anything
        self.expect(
            there is not None and here >= there,
            f"{spec.provider} ranked {here} here, {there} in the clean twin",
        )

    async def _expect_reader(self, step: ExpectReader) -> None:
        spec = step.expect_reader
        reads = self._last_records("reader.call")
        labels = [lb for r in reads for lb in (r.get("labels") or [])]
        flagged = {lb["review_id"] for lb in labels if lb.get("injection_suspected")}
        for rid in spec.flagged:
            self.expect(rid in flagged, f"reader did not flag {rid} (flagged {sorted(flagged)})")
        for rid in spec.not_flagged:
            self.expect(rid not in flagged, f"reader flagged {rid}")
        if spec.none_flagged:
            self.expect(bool(labels), "reader produced no labels at all")
            self.expect(not flagged, f"reader flagged {sorted(flagged)}")
        candidates = await self._shortlist()
        self.expect(bool(candidates), "no shortlist to check the review signal on")
        wanted = spec.no_review_bonus
        for c in candidates:
            if wanted == ["*"] or c["provider_id"] in wanted:
                signal = c.get("score_parts", {}).get("review_signal")
                self.expect(signal == 0, f"{c['provider_id']}: review_signal {signal}")
        missing = [
            p for p in wanted if p != "*" and p not in {c["provider_id"] for c in candidates}
        ]
        self.expect(not missing, f"no review bonus check: {missing} not on the shortlist")
        options = self._card_options()
        for pid, badge in spec.badge.items():
            got = (options.get(pid) or {}).get("badges", [])
            self.expect(badge in got, f"card for {pid} has badges {got}, wanted {badge}")
        for pid in spec.notice:
            option = options.get(pid) or {}
            self.expect(
                bool(option.get("notice")) and option.get("injection_flags", 0) >= 1,
                f"card for {pid} has no hidden-review notice",
            )
        errors = Counter(str(r.get("error")) for r in reads if r.get("error"))
        for error, n in spec.errors.items():
            self.expect(errors[error] == n, f"reader errors {dict(errors)}, wanted {error}x{n}")
        if spec.calls is not None:
            ends = self._last_records("run.end")
            calls = ends[-1]["totals"].get("reader_calls") if ends else None
            self.expect(calls == spec.calls, f"reader calls {calls}, wanted {spec.calls}")

    async def _expect_planner(self, step: ExpectPlanner) -> None:
        spec = step.expect_planner
        tried: set[str] = set()
        for record in self._last_records("model.call"):
            for call in record["response"]["tool_calls"]:
                name = str(call["name"])
                tried.add(name)
                pid = (call.get("args") or {}).get("provider_id")
                if pid:
                    tried.add(f"{name}:{pid}")
        for want in spec.called:
            self.expect(want in tried, f"the planner never called {want} (called {sorted(tried)})")
        records = invariants.load_records(self.h.root, await self.h.op_log())
        for record in records.all_records():
            if record["type"] != "model.call":
                continue
            for call in record["response"]["tool_calls"]:
                pid = (call.get("args") or {}).get("provider_id")
                if str(call["name"]).startswith("propose_") and pid in spec.no_proposal_for:
                    self.expect(False, f"{record['run_id']}: {call['name']} named {pid}")

    async def _expect_no_side_effects(self, pending_card_ok: bool = False) -> None:
        counts = Counter(row["op"] for row in await self.h.op_log())
        for op in SIDE_EFFECT_OPS:
            allowed = 1 if pending_card_ok and op == "hold_slot" else 0
            self.expect(counts[op] <= allowed, f"world {op} ops: {counts[op]}")
        seeded = sorted((b.provider, b.status) for b in self.scenario.world_config().user.bookings)
        now = sorted((b.provider_id, b.status) for b in await self.h.inspector.bookings())
        self.expect(now == seeded, f"world bookings {now}, seeded {seeded}")
        for table in ("approvals", "action_intents", "memories"):
            n = self.h.agent_db_count(table)
            allowed = 1 if pending_card_ok and table == "approvals" else 0
            self.expect(n <= allowed, f"agent.sqlite {table}: {n} rows")
        if pending_card_ok:
            statuses = self.h.agent_db_approval_statuses()
            self.expect(statuses == ["pending"], f"approval rows {statuses}, want one pending")

    async def _expect_limit(self, step: ExpectLimit) -> None:
        rec = await self.h.records()
        ends = [r for r in rec.traces[self.last.run_id] if r["type"] == "run.end"]
        named = ends[-1].get("limit") if ends else None
        self.expect(
            named == step.expect_limit, f"run.end names limit {named!r}, wanted {step.expect_limit}"
        )

    async def _expect_not_in_requests(self, step: ExpectNotInRequests) -> None:
        spec = step.expect_not_in_requests
        rec = await self.h.records()
        calls = [r for r in rec.all_records() if r["type"] == "model.call"]
        self.expect(bool(calls), "no model.call records: the check would pass vacuously")
        for call in calls:
            hay = json.dumps(call["request"], ensure_ascii=False)
            for marker in spec.markers:
                self.expect(marker not in hay, f"marker {marker!r} reached a model request")
        asked: set[str] = set()
        for call in calls:
            for tc in call["response"]["tool_calls"]:
                if tc["name"] in spec.no_tool_result_for and tc.get("id"):
                    asked.add(str(tc["id"]))
        for call in calls:
            hay = json.dumps(call["request"], ensure_ascii=False)
            for tool_id in sorted(asked):
                self.expect(
                    f'"tool_use_id": "{tool_id}"' not in hay,
                    f"the planner was given a tool_result for {tool_id}",
                )

    async def _expect_brief(self, step: ExpectBrief) -> None:
        spec = step.expect_brief
        state = (await self.h.conversation())["state"] or {}
        brief = (state.get("errand") or {}).get("brief") or {}
        if spec.version is not None:
            self.expect(
                brief.get("version") == spec.version,
                f"brief v{brief.get('version')}, expected v{spec.version}",
            )
        for name, want in spec.values.items():
            fact = brief.get(name.value) or {}
            got = fact.get("value")
            self.expect(str(got) == str(want), f"brief {name.value}={got!r}, expected {want!r}")
        for name, quote in spec.evidence.items():
            prov = (brief.get(name.value) or {}).get("prov") or {}
            self.expect(
                prov.get("evidence") == quote,
                f"brief {name.value} evidence {prov.get('evidence')!r}, expected {quote!r}",
            )
        for name, turn in spec.set_at_turn.items():
            at = (brief.get(name.value) or {}).get("set_at_turn")
            self.expect(at == turn, f"brief {name.value} set at turn {at}, expected {turn}")

    def _expect_patch(self, step: ExpectPatch) -> None:
        applied = [r for r in self._last_records("brief.patch") if r.get("evidence_ok")]
        got_set = [f for r in applied for f in r["fields"]["set"]]
        got_clear = [f for r in applied for f in r["fields"]["clear"]]
        want = step.expect_patch
        self.expect(
            got_set == [f.value for f in want.set] and got_clear == [f.value for f in want.clear],
            f"{self.last.run_id}: brief.patch set {got_set} clear {got_clear}",
        )

    def _expect_shortlist(self, step: ExpectShortlist) -> None:
        card = next(
            (m["body"] for m in self.last.messages if m["body"]["type"] == "shortlist"), None
        )
        self.expect(card is not None, f"{self.last.run_id}: no shortlist card")
        if card is None:
            return
        spec = step.expect_shortlist
        if spec.window_label_includes is not None:
            self.expect(
                spec.window_label_includes in card["window_label"],
                f"shortlist window {card['window_label']!r}, wanted {spec.window_label_includes!r}",
            )
        if spec.slot_weekdays is not None:
            days = {
                datetime.fromisoformat(slot["start"]).isoweekday()
                for c in card["candidates"]
                for slot in c["slots"]
            }
            self.expect(
                bool(days) and days <= set(spec.slot_weekdays),
                f"shortlist slots fall on weekdays {sorted(days)}, wanted {spec.slot_weekdays}",
            )
        if spec.slot_hours is not None:
            lo, hi = (time.fromisoformat(h) for h in spec.slot_hours)
            slots = [s for c in card["candidates"] for s in c["slots"]]
            outside = [
                s["label"]
                for s in slots
                if datetime.fromisoformat(s["start"]).time() < lo
                or datetime.fromisoformat(s["end"]).time() > hi
            ]
            self.expect(
                bool(slots) and not outside,
                f"shortlist slots outside {spec.slot_hours[0]}-{spec.slot_hours[1]}: {outside}",
            )
        if spec.chips is not None:
            names = [c["field"] for c in card["brief"]]
            self.expect(names == [f.value for f in spec.chips], f"shortlist brief chips {names}")
        if spec.score_parts:
            missing = [c["provider_id"] for c in card["candidates"] if not c["score_parts"]]
            self.expect(not missing, f"candidates without score_parts: {missing}")
        if spec.excluded_reasons is not None:
            got = sorted({x["reason"] for x in card["excluded"]})
            self.expect(got == sorted(spec.excluded_reasons), f"excluded reasons {got}")
            self.expect(
                len(card["excluded"]) == card["excluded_count"]
                and all(x["reason_text"] for x in card["excluded"]),
                "excluded list and count disagree",
            )

    async def _expect_approvals(self, step: ExpectApprovals) -> None:
        conn = sqlite3.connect(f"file:{self.h.root / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT status, invalidated_reason FROM approvals ORDER BY requested_at, rowid"
            ).fetchall()
        finally:
            conn.close()
        got = [f"{status}:{reason}" if reason else status for status, reason in rows]
        self.expect(
            got == step.expect_approvals, f"approvals {got}, wanted {step.expect_approvals}"
        )

    async def _expect_book_calls(self, step: ExpectBookCalls) -> None:
        spec = step.expect_book_calls
        rec = await self.h.records()
        rows = [r for r in rec.op_log if r["op"] == "book"]
        agent = [
            r for r in rec.all_records() if r["type"] == "tool.call" and r.get("tool") == "book"
        ]
        args = [json.loads(r["args_json"]) for r in rows]
        got = {
            "agent_calls": len(agent),
            "world_rows": len(rows),
            "keys": len({a.get("idempotency_key") for a in args}),
            "holds": len({a.get("hold_id") for a in args}),
            "outcomes": [r["outcome"] for r in rows],
        }
        for name, want in spec.model_dump(exclude_none=True).items():
            self.expect(got[name] == want, f"book {name}: {got[name]}, want {want}")

    async def _post(self, step: Post) -> None:
        spec = step.post
        if spec.concurrent:
            responses = await asyncio.gather(
                *[self.h.post(spec.text, spec.client_msg_id) for _ in range(spec.times)]
            )
        else:
            responses = [
                await self.h.post(spec.text, spec.client_msg_id) for _ in range(spec.times)
            ]
        statuses = [r.status_code for r in responses]
        self.expect(
            all(s in (200, 202) for s in statuses), f"{spec.client_msg_id}: statuses {statuses}"
        )
        run_ids = {r.json().get("run_id") for r in responses if r.status_code in (200, 202)}
        self.expect(len(run_ids) == 1, f"{spec.client_msg_id}: run ids {sorted(map(str, run_ids))}")
        if len(run_ids) != 1:
            return
        run_id = str(next(iter(run_ids)))
        view = await self.h.finish(run_id)
        self.user.said(spec.gives)
        await self._said(view)
        convo = await self.h.conversation()
        rows = [m for m in convo["messages"] if m["role"] == "user" and m["run_id"] == run_id]
        self.expect(len(rows) == 1, f"{spec.client_msg_id}: {len(rows)} user message rows")
        starts = [e for e in view.events if e.event == "run.started"]
        self.expect(len(starts) == 1, f"{run_id}: {len(starts)} run.start records")
        runs = [r for r in convo["runs"] if r["run_id"] == run_id]
        self.expect(len(runs) == 1, f"{run_id}: {len(runs)} runs rows")


@dataclass(frozen=True)
class TwinResult:
    order: list[str]  # provider ids on the twin's last shortlist, best first


def _actual(
    runs: list[RunView], op_log: list[dict[str, Any]], violations: dict[str, list[str]]
) -> str:
    parts = []
    for view in runs:
        card = view.card()
        asked = f" [{', '.join(f.value for f in card.fields)}]" if card else ""
        parts.append(f"{view.outcome}{asked}")
    counts = Counter(row["op"] for row in op_log)
    world = ", ".join(f"{op}={n}" for op, n in sorted(counts.items())) or "no calls"
    bad = [f"{name}: {len(v)}" for name, v in violations.items() if v]
    inv = "invariants ok" if not bad else "violations " + "; ".join(bad)
    return f"runs {' -> '.join(parts)}; world {world}; {inv}"


def trace_name(
    scenario: Scenario, replay: ReplayRun | None = None, mode: Dedupe | None = None
) -> str:
    """The directory under evals/out that holds this row's traces: `<id>.replay` for a
    replay row, else the dedupe-mode folder."""
    return f"{scenario.id}.replay" if replay else trace_dir(scenario, mode)


def _notes(scenario: Scenario, records: invariants.Records, replay: ReplayRun | None = None) -> str:
    model_calls = tool_calls = reader_calls = max_est = 0
    cost = 0.0
    for record in records.all_records():
        if record["type"] == "model.call":
            max_est = max(max_est, int(record["est_request_tokens"]))
        if record["type"] == "run.end":
            totals = record["totals"]
            model_calls += int(totals["model_calls"])
            tool_calls += int(totals["tool_calls"])
            reader_calls += int(totals.get("reader_calls", 0))
            cost += float(totals["cost_usd"])
    notes = records.notes
    reviews = notes.get("review_texts_checked", "not checked")
    # The counts behind the M4 invariants, so a check that looked at nothing shows.
    checked = (
        f"book_ops_checked={notes.get('book_ops_checked', 'not checked')}; "
        f"approvals_checked={notes.get('approvals_checked', 'not checked')}; "
        f"typed_grants_checked={notes.get('typed_grants_checked', 'not checked')}; "
        f"claims_checked={notes.get('claims_checked', 'not checked')}; "
        f"inv03_books={notes.get('inv03_books_checked', 'not checked')}; "
        f"inv03_resends={notes.get('inv03_resends_checked', 'not checked')}; "
        f"inv03_liveness={notes.get('inv03_liveness_checked', 'not checked')}; "
        f"inv03_bounds={notes.get('inv03_count_bounds_checked', 'not checked')}; "
        "never_mind=vacuous until M6; "
        f"attempts_checked={notes.get('attempts_checked', 'not checked')}; "
        f"attempts_unsettled={notes.get('attempts_unsettled', 'not checked')}; "
    )
    return (
        f"model_calls={model_calls}; reader_calls={reader_calls}; tool_calls={tool_calls}; "
        f"max_est_tokens={max_est}; "
        f"cost_usd={cost:.6f}; review_texts_checked={reviews}; {checked}"
        f"trace=backend/evals/out/{trace_name(scenario, replay)}/"
    )


def trace_dir(scenario: Scenario, mode: Dedupe | None = None) -> str:
    """The folder under evals/out for one run: the scenario id, plus `.dedupe-off` for a
    scenario that runs in more than one mode."""
    label = scenario.label(mode or scenario.modes()[0])
    return label.replace("[dedupe=", ".dedupe-").rstrip("]")


def save_traces(
    scenario: Scenario,
    root: Path,
    replay: ReplayRun | None = None,
    mode: Dedupe | None = None,
) -> None:
    dest = Path(os.environ.get(TRACE_DIR_ENV) or OUT) / trace_name(scenario, replay, mode)
    if dest.exists():
        shutil.rmtree(dest)
    if (root / "traces").exists():
        shutil.copytree(root / "traces", dest)


async def run_scenario(
    scenario: Scenario,
    root: Path,
    policy: Policy | None = None,
    on_records: Callable[[invariants.Records], None] | None = None,
    replay: ReplayRun | None = None,
    mode: Dedupe | None = None,
) -> ScenarioResult:
    """One run of `scenario` in one dedupe mode (the first it declares, unless given),
    scripted or, with `replay`, on a cassette."""
    mode = mode or scenario.modes()[0]
    scenario = scenario.for_mode(mode)
    expectations = [d for d in (describe(s, mode) for s in scenario.steps) if d]
    names = scenario.expect.invariants
    expanded = [n for name in names for n in (invariants.DEFAULT if name == "default" else [name])]
    expected = "; ".join(expectations) + f"; {', '.join(dict.fromkeys(expanded))} hold"
    runner: Runner | None = None
    failures: list[str] = []
    planner = policy or PLANNERS[scenario.model]
    reader = reader_doubles.READERS[scenario.reader]
    extra = replay.harness_kwargs() if replay else {}
    async with scenario_harness(scenario, root, planner, reader, **extra) as h:
        runner = Runner(scenario, h, planner, mode)
        started = monotonic()
        try:
            await runner.run()
        except SimUserFailure as exc:
            failures.append(f"simulated user: {exc}")
        except AssertionError as exc:
            failures.append(str(exc))
        took = monotonic() - started
        cap = scenario.expect.max_real_s
        if cap is not None and took > cap:
            failures.append(f"the scenario took {took:.1f} s of real time, over {cap:g} s")
        records = await h.records()
        op_log = records.op_log
    if on_records is not None:
        on_records(records)
    violations = invariants.check(records, names)
    failures += runner.failures
    failures += [f"{name}: {v}" for name, vs in violations.items() for v in vs]
    if replay:
        failures += replay.extra_failures(records)
    save_traces(scenario, root, replay, mode)
    actual = _actual(runner.runs, op_log, violations)
    checks = [f for f in failures if not f.startswith("INV-")]
    if checks:
        actual += f"; {len(checks)} expectation(s) failed"
    return ScenarioResult(
        scenario=replay.scenario_id(scenario) if replay else scenario.label(mode),
        category=", ".join(scenario.category),
        model_mode=replay.model_mode if replay else scenario.model_mode,
        world=scenario.world_label(),
        expected=expected,
        actual=actual,
        passed=not failures,
        notes=(replay.notes(records) if replay else "") + _notes(scenario, records, replay),
        failures=failures,
    )
