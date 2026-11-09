"""LoopForeverPolicy (S44, S45): a planner that never stops asking for more.

Its first response records the brief from the opener's words (so a limit notice has
something true to say about "so far"), and every response after that is a batch of
`list_bookings` calls with no `end_turn`. Which cap binds depends on the batch size and
the scenario's `limits:` (iterations, model calls or tool calls). Nothing here can reach
a consequential tool: `list_bookings` is a read.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies.common import read, tool_calls
from evals.policies.reference import extract, update_brief_args


def loop_forever(calls_per_response: int = 1) -> Callable[[ScriptedRequest], AIMessage]:
    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if not view.loop:
            patch = extract(view.current, view)
            if patch.values:
                return tool_calls(
                    request,
                    "Record what the user told me first.",
                    ("update_brief", update_brief_args(patch)),
                )
        return tool_calls(request, "Once more.", *[("list_bookings", {})] * calls_per_response)

    return policy


loop_forever_policy = loop_forever(1)
loop_forever_x3_policy = loop_forever(3)
