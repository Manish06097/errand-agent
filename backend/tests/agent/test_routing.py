"""Input routing: plain input normally, Command(resume=...) when a run is parked.

M3 has no interrupt yet; this proves the admission check is in place for M4 by
parking a thread at a real interrupt (spike L5: plain input there would restart from
START and drop the interrupt).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from errand.agent.graph import Graph, route_input, run_config
from errand.agent.state import ThreadState
from errand.domain.inbound import UserMessage


def park(state: ThreadState) -> dict[str, Any]:
    interrupt({"approval_id": "ap_1"})
    return {}


async def test_a_parked_thread_is_resumed_and_a_fresh_one_gets_plain_input(
    tmp_path: Path,
) -> None:
    builder: StateGraph[ThreadState, Any, ThreadState, ThreadState] = StateGraph(ThreadState)
    builder.add_node("park", park)
    builder.add_edge(START, "park")
    builder.add_edge("park", END)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as saver:
        graph = cast(Graph, builder.compile(checkpointer=saver))
        inbound = UserMessage(message_id="r_0002-u", text="book it", turn_no=2)
        run_input = {"thread_id": "t1", "user_id": "u", "run_id": "r_0002", "inbound": inbound}

        fresh, routed = await route_input(graph, "t1", run_input)
        assert routed == "input" and fresh is run_input

        await graph.ainvoke(
            cast(Any, {"thread_id": "t1", "user_id": "u", "run_id": "r_0001"}),
            run_config("t1"),
            durability="sync",
        )
        resumed, routed = await route_input(graph, "t1", run_input)
        assert routed == "resume"
        assert isinstance(resumed, Command)
        assert resumed.resume == inbound.model_dump(mode="json")
        assert resumed.update == run_input
