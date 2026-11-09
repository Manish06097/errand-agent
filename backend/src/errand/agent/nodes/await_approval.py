"""await_approval: the graph's only interrupt (architecture 6.2, G1).

A resumed node runs again from its first line (spike L3), so this node does nothing
before `interrupt()` and nothing after it except hand the resume value on: no trace
record, no read, no write. The value is the inbound as JSON; the state schema turns it
back into its `Inbound` type when `ingress` reads it. Everything the resume means is
decided in `ingress`, which re-reads the approval row, so the value is only a hint.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState


async def await_approval(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    inbound = interrupt({"approval_id": state.errand.approval_id if state.errand else None})
    return {"inbound": inbound}
