"""Hard limits and outages end in a templated message, one run.end and a thread that still
works, through the real API (architecture 15.1, 15.2; N3 part A)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from errand.agent import graph as graph_module
from errand.config import LimitSettings
from errand.llm.scripted import ScriptedChatModel, sdk_failure
from evals import invariants
from evals.runner import harness
from tests.api.support import OPENER, Gate, GatedModel, world
from tests.api.test_node_exceptions import OPENER as BOOK_OPENER
from tests.api.test_node_exceptions import world as booking_world


def failing(kind: str) -> Any:
    return lambda: ScriptedChatModel(script=[sdk_failure(kind)])


@pytest.mark.parametrize(
    "kind", ["overloaded", "server_error", "rate_limit", "connection", "timeout"]
)
async def test_a_model_outage_after_the_sdk_retries_is_model_unavailable(
    kind: str, tmp_path: Path
) -> None:
    async with harness(tmp_path, world(), model_factory=failing(kind)) as h:
        view = await h.say(OPENER)
        rec = await h.records()
        again = await h.say("hello")  # the thread still works (the script has run out: 500)
    assert view.outcome == "MODEL_UNAVAILABLE"
    notice = view.notice()
    assert notice is not None
    assert notice["text"] == (
        "I can't reach my planning model right now, so I stopped. Nothing was booked."
    )
    assert [a["label"] for a in notice["actions"]] == ["Try again"]
    calls = [r for r in rec.traces[view.run_id] if r["type"] == "model.call"]
    assert len(calls) == 1 and calls[0]["error"] and calls[0]["usage"]["output"] == 0
    assert invariants.inv07_one_end_per_run(rec) == []
    assert again.outcome is not None


async def test_a_400_from_the_api_is_our_bug_and_stays_internal_error(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), model_factory=failing("bad_request")) as h:
        view = await h.say(OPENER)
    assert view.outcome == "INTERNAL_ERROR"


def gated(gate: Gate) -> Any:
    return lambda: GatedModel(script=[AIMessage(content="never sent")], gate=gate)


async def test_a_planner_call_timing_out_with_time_left_is_model_unavailable(
    tmp_path: Path,
) -> None:
    gate = Gate()  # never opened: the call hangs until the planner timeout cuts it
    limits = LimitSettings(planner_timeout_s=0.1)
    async with harness(tmp_path, world(), model_factory=gated(gate), limits=limits) as h:
        view = await h.say(OPENER)
    assert view.outcome == "MODEL_UNAVAILABLE"


async def test_a_planner_call_cut_by_the_deadline_is_wall_clock_exceeded(tmp_path: Path) -> None:
    gate = Gate()
    # 0.1 s of run time against a 60 s call timeout: the cap is the deadline's.
    limits = LimitSettings(run_deadline_s=0.1)
    async with harness(tmp_path, world(), model_factory=gated(gate), limits=limits) as h:
        view = await h.say(OPENER)
    assert view.outcome == "WALL_CLOCK_EXCEEDED"
    notice = view.notice()
    assert notice is not None and "ran out of time" in notice["text"]


async def test_the_deadline_is_checked_before_every_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = graph_module.plan

    async def slow_plan(state: Any, runtime: Any) -> Any:
        update = await real(state, runtime)
        await runtime.context.clock.sleep(61)  # the proposal is ready, and the time is gone
        return update

    monkeypatch.setattr(graph_module, "plan", slow_plan)
    async with harness(tmp_path, booking_world()) as h:
        view = await h.say(BOOK_OPENER)
        ops = await h.op_log()
        rec = await h.records()
        again_state = await h.conversation()
    assert view.outcome == "WALL_CLOCK_EXCEEDED"
    assert [r["op"] for r in ops if r["op"] == "hold_slot"] == []  # prepare_proposal never ran
    notice = view.notice()
    assert notice is not None and notice["text"].endswith("Nothing was booked.")
    assert "Still to do: " in notice["text"]
    assert invariants.inv07_one_end_per_run(rec) == []
    assert again_state["messages"]


async def test_the_backstop_ends_a_blocked_node_as_wall_clock_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = graph_module.plan
    blocked = {"on": True}

    async def hang(state: Any, runtime: Any) -> Any:
        if blocked["on"]:
            await asyncio.sleep(30)  # a real await the backstop must cut
        return await real(state, runtime)

    monkeypatch.setattr(graph_module, "plan", hang)
    limits = LimitSettings(run_backstop_s=0.3)
    async with harness(tmp_path, world(), limits=limits) as h:
        view = await h.say(OPENER)
        rec = await h.records()
        blocked["on"] = False
        again = await h.say(OPENER)
    assert view.outcome == "WALL_CLOCK_EXCEEDED"
    notice = view.notice()
    assert notice is not None and notice["text"].endswith("Nothing was booked.")
    assert [r["type"] for r in rec.traces[view.run_id]].count("run.end") == 1
    assert again.outcome == "ASKED_USER"
