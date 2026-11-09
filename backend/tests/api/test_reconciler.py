"""The reconciler (N1): re-checks, Check now, the critical section's exits, dead-run
closing and the startup order. The real kill-and-restart is S36, S37 and S52 in the eval
suite; these cover the pieces that do not need a process.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from errand.agent.nodes import verify as verify_node
from errand.api import reconciler as reconciler_module
from errand.api.app import AppConfig, create_app
from errand.api.reconciler import Reconciler
from errand.config import ModelSettings, ReconcilerSettings
from errand.store.repos.runs import Run, RunTotals
from errand.tools.errors import ToolBudgetExceeded
from errand.tools.gateway import ToolGateway
from errand.tools.runtime import in_process
from errand.world.engine import WorldHost
from errand.world.scenario import (
    BookConfig,
    FaultsConfig,
    ScenarioConfig,
    StatusOutage,
    WorldConfig,
)
from evals import invariants
from evals.runner import Harness, harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def status_down_once() -> ScenarioConfig:
    """`book` times out after committing and the first pass of four status checks finds the
    status endpoint down: the user is told it is pending, and the booking exists."""
    return ScenarioConfig(
        id="pending_then_confirmed",
        seed=40,
        world=WorldConfig(book=BookConfig(forced=["timeout_booked"])),
        faults=FaultsConfig(get_booking_status=StatusOutage(unavailable_n=4)),
    )


async def approve_to_pending(h: Harness) -> str:
    view = await h.say(OPENER)
    assert view.outcome == "AWAITING_APPROVAL"
    card = next(m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action")
    click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    run = await h.finish(click.json()["run_id"])
    assert run.outcome == "PENDING_VERIFICATION", run.outcome
    return str(run.run_id)


async def test_a_tick_settles_a_needs_attention_intent_through_the_status_check(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, status_down_once()) as h:
        await approve_to_pending(h)
        async with h.manager.db.session() as repos:
            states = [
                i.state for i in await repos.intents.unresolved_for_conversation(h.conversation_id)
            ]
        assert states == ["needs_attention"]
        ran = await h.reconcile()
        assert len(ran) == 1
        view = await h.finish(ran[0])
        text = json.dumps(view.messages[-1]["body"])
        rec = await h.records()
    assert view.outcome == "BOOKED_VERIFIED"
    # a tick in a live process is not a restart, and says so
    assert "after a restart" not in text
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1
    assert [i["state"] for i in rec.intents] == ["succeeded"]
    for check in (invariants.inv07_one_end_per_run, invariants.inv08_transitions_allowed):
        assert check(rec) == []


async def test_a_tick_with_nothing_to_check_starts_no_run(tmp_path: Path) -> None:
    async with harness(tmp_path, ScenarioConfig(id="idle", seed=1)) as h:
        assert await h.reconcile() == []
        rec = await h.records()
    assert rec.runs == []


async def test_check_now_runs_the_same_check_and_is_idempotent(tmp_path: Path) -> None:
    async with harness(tmp_path, status_down_once()) as h:
        await approve_to_pending(h)
        url = f"/api/conversations/{h.conversation_id}/actions"
        body = {"client_action_id": "chk-1", "kind": "check_now", "payload": {}}
        first = await h.client.post(url, json=body)
        assert first.status_code == 202
        again = await h.client.post(url, json=body)
        assert again.status_code == 202 and again.json()["run_id"] == first.json()["run_id"]
        view = await h.finish(first.json()["run_id"])
        # settled now: a new press has nothing to check
        spent = await h.client.post(url, json={**body, "client_action_id": "chk-2"})
        rec = await h.records()
    assert view.outcome == "BOOKED_VERIFIED"
    assert spent.status_code == 409 and spent.json()["error"] == "nothing_to_check"
    assert spent.json()["intent"]["state"] == "succeeded"
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1


async def test_check_now_on_a_conversation_that_never_booked_is_a_409(tmp_path: Path) -> None:
    async with harness(tmp_path, ScenarioConfig(id="none", seed=1)) as h:
        url = f"/api/conversations/{h.conversation_id}/actions"
        response = await h.client.post(
            url, json={"client_action_id": "c", "kind": "check_now", "payload": {}}
        )
    assert response.status_code == 409
    assert response.json() == {"error": "nothing_to_check", "intent": None}


async def test_check_now_after_a_recorded_mismatch_is_a_409_not_a_run(tmp_path: Path) -> None:
    """S65: the world booked 150 rupees above the quote. The intent is needs_attention, but
    what the provider recorded is settled: nothing waits on it, so Check now has nothing to
    check (the same `_WAITING` test the reconciler uses), and no run is started."""
    world = ScenarioConfig(
        id="mismatch",
        seed=65,
        world=WorldConfig(book=BookConfig(forced=["success"])),
        faults=FaultsConfig(book_price_mismatch=150),
    )
    async with harness(tmp_path, world) as h:
        await approve_to_pending(h)
        async with h.manager.db.session() as repos:
            states = [
                i.state for i in await repos.intents.unresolved_for_conversation(h.conversation_id)
            ]
            assert not await repos.intents.awaiting_provider(h.conversation_id)
        url = f"/api/conversations/{h.conversation_id}/actions"
        response = await h.client.post(
            url, json={"client_action_id": "chk-m", "kind": "check_now", "payload": {}}
        )
        rec = await h.records()
    assert states == ["needs_attention"]
    assert response.status_code == 409
    assert response.json()["error"] == "nothing_to_check"
    assert response.json()["intent"]["state"] == "needs_attention"
    assert [r["trigger"] for r in rec.runs].count("ui_action") == 0


async def test_the_tool_call_cap_tripping_inside_verify_still_ends_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is not meant to apply in the critical section; if it ever trips there the
    run ends PENDING_VERIFICATION, never LIMIT_REACHED, and a tick resolves it."""
    world = ScenarioConfig(
        id="cap", seed=41, world=WorldConfig(book=BookConfig(forced=["success"]))
    )
    real = ToolGateway.call
    trip = {"on": True}

    async def capped(self: ToolGateway, caller: Any, tool: Any, args: Any, **kw: Any) -> Any:
        if trip["on"] and tool == "get_booking_status":
            raise ToolBudgetExceeded(tool, "tool_calls")
        return await real(self, caller, tool, args, **kw)

    monkeypatch.setattr(ToolGateway, "call", capped)
    async with harness(tmp_path, world) as h:
        await approve_to_pending(h)
        trip["on"] = False
        ran = await h.reconcile()
        view = await h.finish(ran[0])
        rec = await h.records()
    assert view.outcome == "BOOKED_VERIFIED"
    assert "LIMIT_REACHED" not in {r["outcome"] for r in rec.runs}
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1


async def test_an_exception_inside_verify_with_an_intent_open_ends_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ScenarioConfig(
        id="boom", seed=42, world=WorldConfig(book=BookConfig(forced=["success"]))
    )
    real = verify_node._Verify.run
    boom = {"on": True}

    async def exploding(self: Any) -> Any:
        if boom["on"]:
            raise RuntimeError("verify blew up")
        return await real(self)

    monkeypatch.setattr(verify_node._Verify, "run", exploding)
    async with harness(tmp_path, world) as h:
        view = await h.say(OPENER)
        assert view.outcome == "AWAITING_APPROVAL"
        card = next(
            m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action"
        )
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        run = await h.finish(click.json()["run_id"])
        said = (await h.conversation())["messages"][-1]["body"]
        async with h.manager.db.session() as repos:
            intent = await repos.intents.get_by_approval(card["approval_id"])
        boom["on"] = False
        ran = await h.reconcile()
        after = await h.finish(ran[0])
        rec = await h.records()
    assert run.outcome == "PENDING_VERIFICATION"
    assert said["outcome"] == "PENDING_VERIFICATION"
    assert [a["kind"] for a in said["actions"]] == ["check_now"]
    assert intent is not None and intent.state == "needs_attention"
    assert after.outcome == "BOOKED_VERIFIED"
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1
    assert invariants.inv07_one_end_per_run(rec) == []


# ------------------------------------------------------------------ dead runs


def _dead_run(tmp_path: Path, lines: list[str], *, torn: str = "") -> tuple[Run, Path]:
    run = Run(
        run_id="r_0009",
        conversation_id="c_0001",
        trigger="user_message",
        status="running",
        outcome=None,
        totals=RunTotals(0, 0, 0, 0, 0.0),
        trace_path=None,
        started_at=0,
        ended_at=None,
    )
    path = tmp_path / "r_0009.jsonl"
    path.write_text("".join(line + "\n" for line in lines) + torn)
    return run, path


def _start_record(seq: int = 1) -> str:
    return json.dumps(
        {
            "v": 1,
            "seq": seq,
            "ts": "2026-11-09T08:00:00.000+05:30",
            "wall_ts": "2026-11-09T02:30:00.000+00:00",
            "run_id": "r_0009",
            "thread_id": "c_0001",
            "errand_id": None,
            "trace_id": "t",
            "span_id": "s_1",
            "parent_span_id": None,
            "node_attempt": 1,
            "type": "run.start",
            "trigger": "user_message",
            "user_message_id": "r_0009-u",
        }
    )


def test_a_dead_runs_torn_last_line_is_dropped_and_run_end_resumes_after_the_last_whole_record(
    tmp_path: Path,
) -> None:
    from datetime import datetime

    from errand.clock import IST

    run, path = _dead_run(tmp_path, [_start_record()], torn='{"v":1,"seq":2,"ts":"2026-1')
    reconciler_module._append_run_end(path, run, "r_0010", datetime(2026, 11, 9, 8, 5, tzinfo=IST))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["run.start", "run.end"]
    end = records[-1]
    assert end["seq"] == 2 and end["span_id"] == "s_1"
    assert end["outcome"] == "RUN_INTERRUPTED" and end["recovered_by"] == "r_0010"
    assert end["user_message_id"] == "r_0009-u"


def test_a_run_that_already_ended_gets_no_second_run_end(tmp_path: Path) -> None:
    from datetime import datetime

    from errand.clock import IST

    end = json.loads(_start_record(2))
    end.update(
        {
            "type": "run.end",
            "outcome": "REPLIED",
            "totals": {
                "model_calls": 0,
                "tool_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "latency_ms": 0.0,
                "reader_calls": 0,
            },
        }
    )
    end.pop("trigger")
    run, path = _dead_run(tmp_path, [_start_record(), json.dumps(end)])
    reconciler_module._append_run_end(path, run, None, datetime(2026, 11, 9, 8, 5, tzinfo=IST))
    assert sum(1 for line in path.read_text().splitlines() if '"run.end"' in line) == 1


# ------------------------------------------------------------------ startup order


async def test_the_reconciler_finishes_before_the_app_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []
    real = Reconciler.startup

    async def slow_startup(self: Reconciler) -> Any:
        await asyncio.sleep(0.3)
        done.append("reconciled")
        return await real(self)

    monkeypatch.setattr(Reconciler, "startup", slow_startup)
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(ScenarioConfig(id="startup", seed=1), "sim")
    try:
        app = create_app(
            AppConfig(
                data_dir=tmp_path,
                world=in_process(host),
                model_settings=ModelSettings(model_mode="scripted"),
                reconciler=ReconcilerSettings(interval_s=0),
            )
        )
        async with app.router.lifespan_context(app):
            # inside the lifespan means the app is serving: the pass has already ended
            assert done == ["reconciled"]
            assert app.state.reconciler is not None
    finally:
        host.close()


async def test_the_periodic_task_settles_a_pending_intent_without_being_asked(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, status_down_once()) as h:
        await approve_to_pending(h)
        # the lifespan did not start the task (the harness sets no interval): start one fast
        periodic = Reconciler(h.manager, ReconcilerSettings(interval_s=0.05, recheck_after_s=0))
        periodic.start_periodic()
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                async with h.manager.db.session() as repos:
                    open_ = await repos.intents.unresolved_for_conversation(h.conversation_id)
                if not open_:
                    break
            await h.manager.wait_idle()
        finally:
            await periodic.stop()
        rec = await h.records()
    assert [i["state"] for i in rec.intents] == ["succeeded"]
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1
    assert [r["trigger"] for r in rec.runs][-1] == "recovery"


async def test_a_check_that_could_not_be_finished_is_checked_again_by_the_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5 promises "I'll keep checking and update you here" for `check_incomplete` (the
    booking was seen, `list_bookings` was down). Nothing re-checked it until N1: the tick
    asks again, the booking is verified, and the user is told."""
    from errand.tools.errors import ToolTransient
    from errand.tools.gateway import Caller

    real = ToolGateway.call
    down = {"on": True}

    async def call(self: ToolGateway, caller: Any, tool: Any, args: Any, **kw: Any) -> Any:
        if down["on"] and caller is Caller.VERIFIER and tool == "list_bookings":
            raise ToolTransient("list_bookings", 503, b"")
        return await real(self, caller, tool, args, **kw)

    monkeypatch.setattr(ToolGateway, "call", call)
    world = ScenarioConfig(
        id="incomplete", seed=43, world=WorldConfig(book=BookConfig(forced=["success"]))
    )
    async with harness(tmp_path, world) as h:
        await approve_to_pending(h)
        notice = (await h.conversation())["messages"][-1]["body"]
        assert notice["pending"]["reason"] == "check_incomplete"
        assert [a["kind"] for a in notice["actions"]] == ["check_now"]
        down["on"] = False
        ran = await h.reconcile()
        view = await h.finish(ran[0])
        rec = await h.records()
    assert view.outcome == "BOOKED_VERIFIED"
    assert [i["state"] for i in rec.intents] == ["succeeded"]
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1


async def test_the_periodic_task_leaves_a_recently_checked_intent_alone_until_it_is_due(
    tmp_path: Path,
) -> None:
    """A still-pending intent would otherwise get a new pending message every interval.
    The periodic rule waits `recheck_after_s` after the last answer; an explicit tick
    never waits."""
    async with harness(tmp_path, status_down_once()) as h:
        await approve_to_pending(h)
        careful = Reconciler(h.manager, ReconcilerSettings(interval_s=30, recheck_after_s=120))
        assert (await careful.tick(due_only=True)).recovery_runs == []
        await h.inspector.advance(121)  # the world and the agent clock share it
        due = await careful.tick(due_only=True, wait=True)
        assert len(due.recovery_runs) == 1
        rec = await h.records()
    assert [r["trigger"] for r in rec.runs] == [
        "user_message",
        "approval_decision_resume",
        "recovery",
    ]


async def test_startup_recovers_every_stranded_intent_of_a_conversation_one_run_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation with a second unresolved intent (the dead run left an `open` one
    beside the one it was settling) gets a recovery run for each, in order, never two at
    once on the thread; one that stays unresolved after its run does not come round again."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from errand.approvals import service
    from errand.domain.inbound import Recover

    def intent(intent_id: str, conversation_id: str) -> Any:
        return SimpleNamespace(
            intent_id=intent_id, conversation_id=conversation_id, user_id="u", state="open"
        )

    first, second, other = intent("i1", "c1"), intent("i2", "c1"), intent("i3", "c2")
    unresolved = [first, second, other]  # nothing ever resolves: the loop must still end
    events: list[str] = []

    async def running() -> list[Any]:
        return []

    async def recoverable() -> list[Any]:
        return list(unresolved)

    @asynccontextmanager
    async def session() -> Any:
        yield SimpleNamespace(
            runs=SimpleNamespace(list_running=running),
            intents=SimpleNamespace(recoverable=recoverable),
        )

    async def no_grants(_: Any) -> list[Any]:
        return []

    async def wait_idle() -> None:
        events.append("idle")

    async def recover(
        self: Reconciler, conversation_id: str, inbound: Any, dead: Any, report: Any, **kw: Any
    ) -> str:
        assert isinstance(inbound, Recover) and inbound.after_restart
        events.append(f"{conversation_id}:{inbound.intent_id}")
        return f"r_{inbound.intent_id}"

    monkeypatch.setattr(service, "expire_unexecuted_grants", no_grants)
    monkeypatch.setattr(Reconciler, "_recover", recover)
    manager = SimpleNamespace(db=SimpleNamespace(session=session), wait_idle=wait_idle)
    report = await Reconciler(manager).startup()  # type: ignore[arg-type]
    assert events == ["c1:i1", "c2:i3", "idle", "c1:i2", "idle", "idle"]  # last: startup's own
    assert report.recovery_runs == ["r_i1", "r_i3", "r_i2"]


async def test_the_periodic_task_stops_asking_after_its_window_but_check_now_does_not() -> None:
    """A contradiction that never resolves, or a status endpoint that never comes back,
    would otherwise get a new pending message every `recheck_after_s` for ever."""
    from types import SimpleNamespace

    async def no_attempts(_: str) -> list[Any]:
        return []

    repos = SimpleNamespace(intents=SimpleNamespace(attempts=no_attempts))
    settings = ReconcilerSettings(recheck_after_s=120, periodic_window_s=1200)
    reconciler = Reconciler(SimpleNamespace(), settings)  # type: ignore[arg-type]

    def stuck(age_s: int, quiet_s: int) -> Any:
        now = 10_000_000
        return now, SimpleNamespace(
            intent_id="i", created_at=now - age_s * 1000, updated_at=now - quiet_s * 1000
        )

    async def due(age_s: int, quiet_s: int) -> bool:
        now, intent = stuck(age_s, quiet_s)
        return await reconciler._due(repos, intent, now)

    assert not await due(age_s=300, quiet_s=60)  # asked a minute ago
    assert await due(age_s=300, quiet_s=121)  # quiet for a whole interval
    assert await due(age_s=1200, quiet_s=121)  # the last periodic answer
    assert not await due(age_s=1201, quiet_s=121)  # past the window: periodic stops
    assert not await due(age_s=86_400, quiet_s=86_400)
