"""Nodes on the booking path are safe to run again from their first line (spike L3).

- prepare_proposal, run twice from the same checkpointed state (a crash after its
  approval insert, then the re-run): one world hold, one holds row, one approval row,
  the same approval id both times. Its ids are derived from the checkpointed run id,
  so a random id would show up here as a second approval.
- await_approval does nothing but interrupt: its body is the interrupt and the return,
  it takes no runtime (so it cannot reach the database, the tracer or the world), and a
  thread parked on it resumes twice in a row without anything having been written by it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from errand.agent.deps import RunDeps
from errand.agent.graph import run_config
from errand.agent.nodes import await_approval as await_module
from errand.agent.nodes.prepare_proposal import prepare_proposal
from errand.agent.state import ThreadState
from errand.config import LimitSettings
from errand.domain.inbound import UserMessage
from errand.domain.outcomes import CardReady, Proposal
from errand.tools.gateway import ToolGateway
from errand.trace.sinks.null import NullSink
from errand.trace.tracer import Tracer
from errand.world.scenario import ScenarioConfig
from evals.runner import harness

FIND = "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


async def test_prepare_proposal_run_twice_makes_one_hold_and_one_approval(tmp_path: Path) -> None:
    # Imported here: langgraph must load after errand, which turns strict msgpack on.
    from langgraph.runtime import Runtime

    async with harness(tmp_path, ScenarioConfig(id="rerun", seed=5)) as h:
        view = await h.say(FIND)
        assert view.outcome == "PRESENTED_OPTIONS"
        manager = h.manager
        snapshot = await manager.graph.aget_state(run_config(h.conversation_id))
        state = ThreadState.model_validate(snapshot.values)
        assert state.errand is not None and state.errand.shortlist is not None
        first = state.errand.shortlist.candidates[0]
        state = state.model_copy(
            update={
                "run_id": "r_0042",
                "inbound": UserMessage(message_id="r_0042-u", text="book S1", turn_no=2),
                "plan_result": Proposal(
                    action="book", provider_id=first.provider_id, slot_ref=first.slots[0].ref
                ),
            }
        )
        results = []
        for attempt in (1, 2):
            tracer = Tracer(run_id=f"r_{attempt}", thread_id=h.conversation_id, sinks=[NullSink()])
            deps = RunDeps(
                run_id=f"r_{9000 + attempt}",  # a new process: nothing per-run carries over
                conversation_id=h.conversation_id,
                user_id="u_demo",
                tracer=tracer,
                llm=None,  # type: ignore[arg-type]
                reader=None,  # type: ignore[arg-type]
                tools=ToolGateway(
                    manager.world.client,
                    tracer=tracer,
                    clock=manager.clock,
                    counters=state.counters,
                ),
                counters=state.counters.model_copy(deep=True),
                db=manager.db,
                clock=manager.clock,
                tz=manager.tz,
                limits=LimitSettings(),
            )
            update = await prepare_proposal(state, Runtime(context=deps))
            results.append(update["result"])
        async with manager.db.session() as repos:
            approvals = await repos.approvals.list_for_conversation(h.conversation_id)
        holds = [r for r in await h.op_log() if r["op"] == "hold_slot"]
    assert all(isinstance(r, CardReady) for r in results)
    assert results[0].approval_id == results[1].approval_id == "ap_r_0042"
    assert [(a.approval_id, a.status) for a in approvals] == [("ap_r_0042", "pending")]
    assert len(holds) == 1


def test_await_approval_is_the_interrupt_and_nothing_else() -> None:
    node = await_module.await_approval
    assert list(inspect.signature(node).parameters) == ["state"]
    tree = ast.parse(textwrap.dedent(inspect.getsource(node)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    body = [s for s in func.body if not isinstance(s, ast.Expr)]  # the docstring, if any
    assert len(body) == 2, ast.dump(func)
    assign, ret = body
    assert isinstance(assign, ast.Assign) and isinstance(assign.value, ast.Call)
    assert isinstance(assign.value.func, ast.Name) and assign.value.func.id == "interrupt"
    assert isinstance(ret, ast.Return)


async def test_a_parked_thread_resumes_and_parks_again_without_await_approval_writing(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, ScenarioConfig(id="resume", seed=5)) as h:
        first = await h.say(FIND.replace("Find", "Book"))
        assert first.outcome == "AWAITING_APPROVAL"
        for text in ("is that provider verified?", "and which one is cheapest?"):
            view = await h.say(text)
            assert view.outcome == "AWAITING_APPROVAL"
        rec = await h.records()
    nodes = [r["node"] for run in rec.traces.values() for r in run if r["type"] == "node.start"]
    assert "await_approval" not in nodes  # it emits nothing, not even a node record
    parked = [
        r["parked_approval_id"]
        for run in rec.traces.values()
        for r in run
        if r["type"] == "run.end"
    ]
    assert parked[-2:] == [parked[-1]] * 2 and parked[-1] is not None
    assert [r["trigger"] for r in rec.runs][-2:] == ["user_message_resume"] * 2
