"""The graph (architecture 6.2), its run settings and the input routing (6.1, spike L5).

M3 wires `ingress -> plan -> respond -> END`; later milestones add their nodes here.

Every invocation goes through `invoke`: `durability="sync"` (spike L1, L2),
`version="v2"` (L7) and `recursion_limit=40`, with `AsyncSqliteSaver` and only `a*`
methods (L6). Strict msgpack is on (the root `errand` package sets it before
langgraph loads), so only types reachable from `ThreadState` deserialise; `build_graph` refuses to
compile without it.

`route_input` decides how an input enters a thread, under the per-thread lock: if the
thread has a pending interrupt, as `Command(resume=...)`; otherwise as plain input.
Plain input on an interrupted thread would silently drop the interrupt and restart
from START (L5). M3 has no interrupt yet, but the check exists so M4's
`await_approval` does not have to restructure admission.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde import _msgpack
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from errand.agent.deps import RunDeps
from errand.agent.nodes.ingress import ingress, route_after_ingress
from errand.agent.nodes.plan import plan
from errand.agent.nodes.respond import respond
from errand.agent.state import ThreadState
from errand.domain.inbound import UserMessage

RECURSION_LIMIT = 40
RoutedAs = Literal["input", "resume"]
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
    g.add_node("respond", respond)
    g.add_edge(START, "ingress")
    g.add_conditional_edges("ingress", route_after_ingress, ["plan", "respond"])
    g.add_edge("plan", "respond")
    g.add_edge("respond", END)
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
        value = inbound.model_dump(mode="json") if isinstance(inbound, UserMessage) else inbound
        return Command(resume=value, update=run_input), "resume"
    return run_input, "input"


async def invoke(
    graph: Graph, thread_id: str, graph_input: dict[str, Any] | Command[Any], deps: RunDeps
) -> ThreadState:
    """One run of the graph with the settings every run uses. Returns the final state."""
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
    if isinstance(value, ThreadState):
        return value
    # Older output shapes (and aget_state) give a dict of channel values.
    return ThreadState.model_validate(value)
