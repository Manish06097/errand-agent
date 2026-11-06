"""Respond: turns the run's result into one code-rendered message and the outcome.

The message is written with an upsert keyed on (run_id, kind), so respond running
again after a crash rewrites the same row instead of posting twice (6.5).
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState
from errand.agent.templates import render
from errand.clock import to_ms
from errand.domain.inbound import UserMessage
from errand.domain.messages import KIND_OF
from errand.ids import message_id_for
from errand.store.repos.messages import Message


async def respond(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    phase = state.errand.phase.value if state.errand else None
    with deps.tracer.node("respond", phase_before=phase) as span:
        outcome, body = render(state.plan_result, state.errand, deps.run_id, deps.tz)
        kind = KIND_OF[body.type]
        turn_no = state.inbound.turn_no if isinstance(state.inbound, UserMessage) else None
        message = Message(
            message_id=message_id_for(deps.run_id, kind),
            conversation_id=deps.conversation_id,
            client_msg_id=None,
            role="assistant",
            kind=kind,
            body=body.model_dump(mode="json"),
            run_id=deps.run_id,
            turn_no=turn_no,
            approval_id=None,
            typed_grant_ok=None,
            rendered_at=None,
            created_at=to_ms(deps.clock.now()),
        )
        async with deps.db.session() as repos:
            await repos.messages.upsert_assistant(message)
            await repos.conversations.touch(deps.conversation_id, message.created_at)
        span.end_fields["phase_after"] = phase
    return {"outcome": outcome}
