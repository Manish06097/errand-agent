"""An exception inside any graph node still ends the run with one run.end, a templated
message carrying the run id, an `error` record naming the node, and a thread that runs the
next message normally (architecture 15.2, N3 ruling 3).

Parametrized over the graph's own node list, and the parameter list is asserted equal to
it, so a node added later is covered or this file goes red.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import errand.agent  # noqa: F401  (sets strict msgpack before langgraph loads)
from errand.agent import graph as graph_module
from errand.agent.graph import build_graph
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals import invariants
from evals.runner import Harness, harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
NODES = [
    "ingress",
    "followup",
    "plan",
    "prepare_proposal",
    "execute",
    "verify",
    "respond",
    "await_approval",
]


def world() -> ScenarioConfig:
    return ScenarioConfig(
        id="node_exceptions", seed=7, world=WorldConfig(book=BookConfig(forced=["success"] * 3))
    )


def test_the_parameter_list_is_the_graphs_node_list() -> None:
    graph = build_graph(InMemorySaver())
    nodes = {n for n in graph.get_graph().nodes if not n.startswith("__")}
    assert set(NODES) == nodes, "a node was added or removed: cover it in this file"
    assert len(NODES) == len(set(NODES))


class Arm:
    """Makes one node raise while armed and delegate to the real node otherwise."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        self.on = False
        real: Callable[..., Any] = getattr(graph_module, name)

        async def patched(state: Any, runtime: Any) -> Any:
            if not self.on:
                return await real(state, runtime)
            if name == "await_approval":
                await real(state, runtime)  # raises GraphInterrupt on the first entry
            raise RuntimeError(f"boom in {name}")

        monkeypatch.setattr(graph_module, name, patched)


async def drive_to(h: Harness, arm: Arm, node: str) -> str:
    """Drive the conversation to `node` and make it raise there. Returns the failed run id."""
    if node == "followup":
        return await _drive_followup(h, arm)
    if node in ("ingress", "plan", "prepare_proposal", "respond"):
        arm.on = True
        view = await h.say(OPENER)
        arm.on = False
        return view.run_id
    view = await h.say(OPENER)
    assert view.outcome == "AWAITING_APPROVAL"
    if node == "await_approval":
        arm.on = True
        view = await h.say("what were the options again?")
        arm.on = False
        return view.run_id
    convo = await h.conversation()
    card = [m for m in convo["messages"] if m["body"].get("type") == "action"][-1]
    arm.on = True
    click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    assert click.status_code == 202, click.text
    run_id = str(click.json()["run_id"])
    await h.finish(run_id)
    arm.on = False
    return run_id


async def _drive_followup(h: Harness, arm: Arm) -> str:
    """Book, let the provider cancel it, and tick the watcher with the node armed: the
    follow-up run belongs to the system, so the failure has no user message to answer."""
    view = await h.say(OPENER)
    convo = await h.conversation()
    card = [m for m in convo["messages"] if m["body"].get("type") == "action"][-1]
    click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    await h.finish(str(click.json()["run_id"]))
    booking = [b for b in await h.inspector.bookings() if b.created_by_key is not None][-1]
    await h.inspector.fire_provider_cancel(booking.booking_id)
    arm.on = True
    result = await h.app.state.watcher.tick()
    await h.manager.wait_idle()
    arm.on = False
    assert view.outcome == "AWAITING_APPROVAL" and result.delivered
    conversation_id = f"cf_{result.delivered[0]}"
    h.conversations["followup"] = conversation_id
    await h.switch("followup")
    async with h.manager.db.session() as repos:
        runs = await repos.runs.list_for_conversation(conversation_id)
    return runs[-1].run_id


@pytest.mark.parametrize("node", NODES)
async def test_an_exception_in_a_node_ends_the_run_cleanly(
    node: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm = Arm(monkeypatch, node)
    async with harness(tmp_path, world()) as h:
        run_id = await drive_to(h, arm, node)
        view = await h.finish(run_id)
        rec = await h.records()
        trace = rec.traces[run_id]
        # The thread is not wedged: the next message runs normally.
        after = await h.say("hello again")
        rec_after = await h.records()

    assert view.outcome == "INTERNAL_ERROR"
    assert [r["type"] for r in trace].count("run.end") == 1
    notice = view.notice()
    assert notice is not None and run_id in notice["text"]
    assert "Nothing was booked or changed" in notice["text"]
    errors = [r for r in trace if r["type"] == "error" and "boom" in r["message"]]
    assert len(errors) == 1
    expected_where = "run_manager" if node == "respond" else f"node:{node}"
    assert errors[0]["where"] == expected_where
    assert node in errors[0]["message"]
    assert after.outcome not in (None, "INTERNAL_ERROR")
    assert invariants.inv07_one_end_per_run(rec_after) == []
