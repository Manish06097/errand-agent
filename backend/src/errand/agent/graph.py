"""The graph (architecture 6.2), its run settings and the input routing (6.1, spike L5).

M4 wires the booking path:

    START -> ingress -> plan | prepare_proposal | execute | respond
    plan -> prepare_proposal (a validated proposal) | respond
    prepare_proposal -> respond | plan (one SystemNote replan per run)
    execute -> verify | prepare_proposal (a lapsed hold or changed material) | respond
    verify -> respond
    respond -> await_approval (a live *pending* approval) | END
                 (a `granted` row whose run could not preflight is not parked on: the user is
                 told to tap again, and the stored resume restarts it; 8.7)
    await_approval -> ingress          the only interrupt

Every invocation goes through `invoke`: `durability="sync"` (spike L1, L2),
`version="v2"` (L7) and `recursion_limit=40`, with `AsyncSqliteSaver` and only `a*`
methods (L6). Strict msgpack is on (the root `errand` package sets it before
langgraph loads), so only types reachable from `ThreadState` deserialise; `build_graph` refuses to
compile without it.

`route_input` decides how an input enters a thread, under the per-thread lock: if the
thread has a pending interrupt, as `Command(resume=...)`; otherwise as plain input.
Plain input on an interrupted thread would silently drop the interrupt and restart
from START (L5), so every kind of input to a parked thread (a message, a decision, a
decline, a slot click, a refresh) goes in as a resume, whatever it is. `invoke` reports
whether the run ended parked from the v2 output's `interrupts` (L7), not from state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde import _msgpack
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel

from errand.agent.deps import RunDeps
from errand.agent.nodes.await_approval import await_approval
from errand.agent.nodes.execute import execute, route_after_execute
from errand.agent.nodes.ingress import ingress, route_after_ingress
from errand.agent.nodes.plan import plan, route_after_plan
from errand.agent.nodes.prepare_proposal import prepare_proposal, route_after_prepare
from errand.agent.nodes.respond import respond, route_after_respond
from errand.agent.nodes.verify import verify
from errand.agent.state import ThreadState

RECURSION_LIMIT = 40
RoutedAs = Literal["input", "resume", "grant"]
Graph = CompiledStateGraph[ThreadState, RunDeps, ThreadState, ThreadState]


def build_graph(checkpointer: BaseCheckpointSaver[Any]) -> Graph:
    if not _msgpack.STRICT_MSGPACK_ENABLED:
        raise RuntimeError(
            "LANGGRAPH_STRICT_MSGPACK is off: langgraph was imported before errand.agent "
            "could set it. Import errand.agent first."
        )
    g: StateGraph[ThreadState, RunDeps, ThreadState, ThreadState] = StateGraph(
        ThreadState, context_schema=RunDeps
    )
    g.add_node("ingress", ingress)
    g.add_node("plan", plan)
    g.add_node("prepare_proposal", prepare_proposal)
    g.add_node("execute", execute)
    g.add_node("verify", verify)
    g.add_node("respond", respond)
    g.add_node("await_approval", await_approval)
    g.add_edge(START, "ingress")
    g.add_conditional_edges(
        "ingress", route_after_ingress, ["plan", "prepare_proposal", "execute", "respond"]
    )
    g.add_conditional_edges("plan", route_after_plan, ["prepare_proposal", "respond"])
    g.add_conditional_edges("prepare_proposal", route_after_prepare, ["plan", "respond"])
    g.add_conditional_edges(
        "execute", route_after_execute, ["verify", "prepare_proposal", "respond"]
    )
    g.add_edge("verify", "respond")
    g.add_conditional_edges("respond", route_after_respond, ["await_approval", END])
    g.add_edge("await_approval", "ingress")
    return g.compile(checkpointer=checkpointer)


def run_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


async def route_input(
    graph: Graph, thread_id: str, run_input: dict[str, Any]
) -> tuple[dict[str, Any] | Command[Any], RoutedAs]:
    """Plain input, or a resume if the thread is parked at an interrupt.

    `run_input` holds the run fields (run_id, inbound, counters). On a resume they go
    in as the command's state update and the inbound is also the resume value, so
    both paths land in `ingress` with the same state.
    """
    snapshot = await graph.aget_state(run_config(thread_id))
    if snapshot.interrupts:
        inbound = run_input.get("inbound")
        value = inbound.model_dump(mode="json") if isinstance(inbound, BaseModel) else inbound
        return Command(resume=value, update=run_input), "resume"
    return run_input, "input"


@dataclass(frozen=True)
class RunOutput:
    state: ThreadState
    # The approval the run is parked on, from the v2 output's interrupts; None if the
    # run ended without one.
    parked_on: str | None


async def invoke(
    graph: Graph, thread_id: str, graph_input: dict[str, Any] | Command[Any], deps: RunDeps
) -> RunOutput:
    """One run of the graph with the settings every run uses."""
    # Plain input is a partial update (the run fields only), not a whole ThreadState: a
    # full state would overwrite the thread's errand with its default.
    output = await graph.ainvoke(
        cast(Any, graph_input),
        run_config(thread_id),
        context=deps,
        durability="sync",
        version="v2",
    )
    value = getattr(output, "value", output)
    state = value if isinstance(value, ThreadState) else ThreadState.model_validate(value)
    parked: str | None = None
    for item in getattr(output, "interrupts", ()) or ():
        payload = getattr(item, "value", None)
        if isinstance(payload, dict) and payload.get("approval_id"):
            parked = str(payload["approval_id"])
    return RunOutput(state=state, parked_on=parked)
