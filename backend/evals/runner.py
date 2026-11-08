"""The scenario runner: the real app over httpx's ASGI transport (architecture 18.1).

Each scenario gets a fresh directory holding all three SQLite files (agent.sqlite,
checkpoints.sqlite, world.sqlite) and the traces, an in-process world on a SimClock,
and the scripted planner. Everything goes through the HTTP API the web app uses:
messages are POSTed, outcomes are read from the run's SSE stream, and the simulated
user reads its questions from the transcript. Invariants then read the three records.

`Harness` is the reusable part; API tests use it directly.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from errand.api.app import AppConfig, create_app, scripted_factory
from errand.api.runs import ModelFactory
from errand.clock import IST
from errand.config import LimitSettings, ModelSettings
from errand.domain.messages import ClarifyCard
from errand.ids import SequentialIds
from errand.llm.scripted import Policy
from errand.tools.inspector import InProcessInspector, row_to_json
from errand.tools.runtime import in_process
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig
from evals import invariants
from evals.dsl import (
    Answer,
    ExpectBrief,
    ExpectNoCard,
    ExpectNoSideEffects,
    ExpectNotice,
    ExpectOutcome,
    ExpectPlanner,
    ExpectQuestionFor,
    ExpectRank,
    ExpectReader,
    ExpectSearchMatchesBrief,
    ExpectToolErrors,
    ExpectTrace,
    ExpectWorld,
    Post,
    Say,
    Scenario,
    Step,
    Twin,
)
from evals.policies import hostile, reference
from evals.policies import reader as reader_doubles
from evals.simuser import SimUser, SimUserFailure

EVALS = Path(__file__).resolve().parent
OUT = EVALS / "out"
BASE_URL = "http://errand.test"

PLANNERS: dict[str, Policy] = {
    "scripted:reference": reference.policy,
    "scripted:injection_follower": hostile.injection_follower,
    "scripted:budget_poisoner": hostile.budget_poisoner,
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
    ) -> None:
        self.root = root
        self.limits = limits or LimitSettings()
        self.world_config = world
        self.policy = policy
        self.model_factory = model_factory or scripted_factory(policy)
        self.reader = reader
        self.reader_factory = reader_factory or scripted_factory(reader)
        self.host = WorldHost(root / "world.sqlite")
        self.inspector = InProcessInspector(self.host)
        self.client: httpx.AsyncClient
        self.conversation_id = ""
        self._stack = AsyncExitStack()
        self._msg_seq = 0

    async def __aenter__(self) -> Harness:
        self.host.load(self.world_config, "sim")
        self._stack.callback(self.host.close)
        self.app = create_app(
            AppConfig(
                data_dir=self.root,
                world=in_process(self.host),
                model_factory=self.model_factory,
                reader_factory=self.reader_factory,
                ids=SequentialIds(),
                model_settings=ModelSettings(model_mode="scripted"),
                limits=self.limits,
                tz=IST,
            )
        )
        await self._stack.enter_async_context(self.app.router.lifespan_context(self.app))
        self.client = await self._stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_URL)
        )
        response = await self.client.post("/api/conversations")
        response.raise_for_status()
        self.conversation_id = response.json()["conversation_id"]
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    @property
    def manager(self) -> Any:
        return self.app.state.manager

    def next_client_msg_id(self) -> str:
        self._msg_seq += 1
        return f"cm_{self._msg_seq:03d}"

    async def post(self, text: str, client_msg_id: str) -> httpx.Response:
        return await self.client.post(
            f"/api/conversations/{self.conversation_id}/messages",
            json={"client_msg_id": client_msg_id, "text": text},
        )

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

    async def records(self) -> invariants.Records:
        await self.manager.wait_idle()
        return invariants.load_records(self.root, await self.op_log())

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
) -> AsyncIterator[Harness]:
    async with Harness(root, world, policy, model_factory, reader, reader_factory, limits) as h:
        yield h


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


def describe(step: Step) -> str | None:
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
        mode = "concurrently" if step.post.concurrent else "in sequence"
        return f"same client_msg_id x{step.post.times} {mode}: 1 run, 1 message, 1 run.start"
    if isinstance(step, Twin):
        return "twin with clean reviews: identical search answers"
    if isinstance(step, ExpectRank):
        return f"{step.expect_rank.provider} ranked no better than in the twin"
    if isinstance(step, ExpectReader):
        return "reader " + _describe_reader(step)
    if isinstance(step, ExpectPlanner):
        spec = step.expect_planner
        parts = []
        if spec.called:
            parts.append("planner tried " + ", ".join(spec.called))
        if spec.no_proposal_for:
            parts.append("no propose_* for " + ", ".join(spec.no_proposal_for))
        return "; ".join(parts) or None
    if isinstance(step, ExpectNoSideEffects):
        return "no hold/book/cancel/reschedule, bookings as seeded, 0 approvals/intents/memories"
    if isinstance(step, ExpectBrief):
        brief = step.expect_brief
        bits = [f"v{brief.version}"] if brief.version is not None else []
        bits += [f"{k.value}={v}" for k, v in brief.values.items()]
        return "brief " + ", ".join(bits)
    return None


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


class Runner:
    def __init__(self, scenario: Scenario, h: Harness, policy: Policy | None = None) -> None:
        self.scenario = scenario
        self.h = h
        self.policy = policy or PLANNERS[scenario.model]
        self.user = SimUser(scenario.user.persona, dict(scenario.user.goal))
        self.runs: list[RunView] = []
        self.failures: list[str] = []
        # The conversation's input steps so far, for a twin to replay.
        self.inputs: list[Step] = []
        self.twin: TwinResult | None = None

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
        if isinstance(step, Say | Answer | Post):
            self.inputs.append(step)
        if isinstance(step, Say):
            view = await self.h.say(step.say, step.client_msg_id)
            self.user.said(step.gives)
            await self._said(view)
        elif isinstance(step, Answer):
            card = self.last.card()
            if card is None:
                raise AssertionError("answer: the last run asked nothing")
            text, gives = self.user.reply(card)
            view = await self.h.say(text)
            self.user.said(gives)
            await self._said(view)
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
            await self._expect_no_side_effects()
        elif isinstance(step, ExpectBrief):
            await self._expect_brief(step)
        else:  # pragma: no cover - the DSL admits nothing else
            raise AssertionError(f"no runner for {step!r}")

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
            runner = Runner(twin_scenario, th, self.policy)
            await runner.run()
            shortlist = await runner._shortlist()
            twin_ops = await th.op_log()
            await th.manager.wait_idle()
        self.failures += [f"twin: {f}" for f in runner.failures]
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

    async def _expect_no_side_effects(self) -> None:
        counts = Counter(row["op"] for row in await self.h.op_log())
        for op in SIDE_EFFECT_OPS:
            self.expect(counts[op] == 0, f"world {op} ops: {counts[op]}")
        seeded = sorted((b.provider, b.status) for b in self.scenario.world_config().user.bookings)
        now = sorted((b.provider_id, b.status) for b in await self.h.inspector.bookings())
        self.expect(now == seeded, f"world bookings {now}, seeded {seeded}")
        for table in ("approvals", "action_intents", "memories"):
            n = self.h.agent_db_count(table)
            self.expect(n == 0, f"agent.sqlite {table}: {n} rows")

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


def _notes(scenario: Scenario, records: invariants.Records) -> str:
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
    reviews = records.notes.get("review_texts_checked", "not checked")
    return (
        f"model_calls={model_calls}; reader_calls={reader_calls}; tool_calls={tool_calls}; "
        f"max_est_tokens={max_est}; "
        f"cost_usd={cost:.6f}; review_texts_checked={reviews}; "
        f"trace=backend/evals/out/{scenario.id}/"
    )


def save_traces(scenario: Scenario, root: Path) -> None:
    dest = OUT / scenario.id
    if dest.exists():
        shutil.rmtree(dest)
    if (root / "traces").exists():
        shutil.copytree(root / "traces", dest)


async def run_scenario(
    scenario: Scenario,
    root: Path,
    policy: Policy | None = None,
    on_records: Callable[[invariants.Records], None] | None = None,
) -> ScenarioResult:
    expectations = [d for d in (describe(s) for s in scenario.steps) if d]
    names = scenario.expect.invariants
    expanded = [n for name in names for n in (invariants.DEFAULT if name == "default" else [name])]
    expected = "; ".join(expectations) + f"; {', '.join(dict.fromkeys(expanded))} hold"
    runner: Runner | None = None
    failures: list[str] = []
    planner = policy or PLANNERS[scenario.model]
    reader = reader_doubles.READERS[scenario.reader]
    async with harness(root, scenario.world_config(), planner, reader=reader) as h:
        runner = Runner(scenario, h, planner)
        try:
            await runner.run()
        except SimUserFailure as exc:
            failures.append(f"simulated user: {exc}")
        except AssertionError as exc:
            failures.append(str(exc))
        records = await h.records()
        op_log = records.op_log
    if on_records is not None:
        on_records(records)
    violations = invariants.check(records, names)
    failures += runner.failures
    failures += [f"{name}: {v}" for name, vs in violations.items() for v in vs]
    save_traces(scenario, root)
    actual = _actual(runner.runs, op_log, violations)
    checks = [f for f in failures if not f.startswith("INV-")]
    if checks:
        actual += f"; {len(checks)} expectation(s) failed"
    return ScenarioResult(
        scenario=scenario.id,
        category=", ".join(scenario.category),
        model_mode=scenario.model_mode,
        world=scenario.world_label(),
        expected=expected,
        actual=actual,
        passed=not failures,
        notes=_notes(scenario, records),
        failures=failures,
    )
