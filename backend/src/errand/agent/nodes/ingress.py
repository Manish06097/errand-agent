"""Ingress: every input lands here first (6.2).

It loads the thread's errand, starts one if there is none (or the last one finished),
resets the per-run fields and routes. A re-run after a crash produces the same result:
the new errand's id is derived from the run id, nothing is written anywhere.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime

from errand.agent.deps import RunDeps
from errand.agent.state import ThreadState
from errand.agent.transitions import INITIAL_PHASE
from errand.domain.brief import Brief, ErrandKind
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UserMessage
from errand.domain.outcomes import Failure
from errand.ids import errand_id_for

TERMINAL = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.ABANDONED})


async def ingress(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    errand = state.errand
    if errand is None or errand.phase in TERMINAL:
        errand = Errand(
            errand_id=errand_id_for(deps.run_id),
            kind=ErrandKind.BOOK,
            phase=INITIAL_PHASE,
            brief=Brief(),
        )
    deps.tracer.errand_id = errand.errand_id
    with deps.tracer.node("ingress", phase_before=errand.phase.value) as span:
        update: dict[str, Any] = {
            "errand": errand,
            "plan_result": None,
            "outcome": None,
            "system_notes": [],
        }
        if not isinstance(state.inbound, UserMessage):
            kind = state.inbound.type if state.inbound is not None else "none"
            update["plan_result"] = Failure(reason=f"inbound {kind} is not handled until M4")
        span.end_fields["phase_after"] = errand.phase.value
    return update


def route_after_ingress(state: ThreadState) -> Literal["plan", "respond"]:
    if isinstance(state.inbound, UserMessage) and state.plan_result is None:
        return "plan"
    return "respond"
