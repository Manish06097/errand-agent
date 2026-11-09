"""One wrapper for every graph node (architecture 15.2, N3 ruling 3).

Registered in one place (`graph.build_graph`), so a node added later is covered without
anyone remembering to. Before a node runs it checks the run's time left; when a node
raises it records an `error` and turns the exception into a result `respond` renders.

- Deadline: before any node except `respond` and `await_approval` (and the booking
  nodes, see `DEADLINE_EXEMPT`), no time left routes straight to `respond` with
  `Limit(deadline)`. Every timeout below is capped by the same time left, so outside the
  critical section a run's elapsed time never passes the deadline (INV-09).
- Exceptions: `Exception` and nothing wider. LangGraph's `GraphBubbleUp` family
  (`GraphInterrupt`, `ParentCommand`) are `Exception` subclasses and are re-raised, or
  the approval interrupt would break. `asyncio.CancelledError` is a `BaseException` and
  passes through on its own.
- `ToolUnavailable` and `ToolUnexpected` become TOOL_UNAVAILABLE, anything else
  INTERNAL_ERROR. The `error` record carries the node, the exception class and the
  message; the message of a tool error names the tool, never a payload.
- A failure inside `respond` is not caught here: `RunManager._fail` writes the template
  and `run.end` (a node that failed while writing the user's message cannot be trusted
  to write it again).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime

from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState
from errand.domain.outcomes import Failure, Limit, PlanResult, ToolDown
from errand.tools.errors import ToolError, ToolUnavailable, ToolUnexpected

Node = Callable[..., Awaitable[dict[str, Any]]]

# Never entered with the deadline check: respond must always run, await_approval only
# parks. execute and verify are the booking critical section (15.1): once a granted
# approval is being executed the deadline is never enforced in the middle of a side
# effect. N1's critical.py replaces this exemption with the open-intent test.
DEADLINE_EXEMPT = frozenset({"respond", "await_approval", "execute", "verify"})
UNGUARDED = frozenset({"respond"})


def failure_result(exc: Exception) -> PlanResult:
    if isinstance(exc, ToolUnavailable):
        return ToolDown(tool=exc.tool, error_class=exc.error_class)
    if isinstance(exc, ToolUnexpected):
        return ToolDown(tool=exc.tool, error_class="ToolUnexpected")
    if isinstance(exc, ToolError):
        return ToolDown(tool=exc.tool, error_class=type(exc).__name__)
    return Failure(reason=f"{type(exc).__name__}")


def _failed(result: PlanResult) -> dict[str, Any]:
    return {"plan_result": result, "result": None, "node_failed": True}


def guard_node(name: str, fn: Node) -> Node:
    if name in UNGUARDED:
        return fn

    @functools.wraps(fn)
    async def guarded(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
        deps = runtime.context
        if name not in DEADLINE_EXEMPT:
            left = deps.counters.time_left_s(deps.clock.now())
            if left is not None and left <= 0:
                return _failed(Limit(limit="deadline"))
        try:
            return await fn(state, runtime)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            deps.tracer.emit(
                "error",
                where=f"node:{name}",
                exception_type=type(exc).__name__,
                message=str(exc)[:2000],
            )
            return _failed(failure_result(exc))

    return guarded


def route_guard(router: Callable[[ThreadState], str]) -> Callable[[ThreadState], str]:
    """A node that failed (or was skipped for the deadline) goes to `respond`, whatever
    its own router would have said."""

    @functools.wraps(router)
    def guarded(state: ThreadState) -> str:
        return "respond" if state.node_failed else router(state)

    return guarded
