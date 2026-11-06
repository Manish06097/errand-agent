"""ThreadState: the LangGraph state of one conversation (architecture 7.1).

Every type a checkpoint can hold is reachable from here, because strict msgpack
allowlists only what `compile()` finds by walking this schema. The transcript,
approvals, intents and memories live in agent.sqlite, not here (4.2), and the planner's
loop messages never reach state (6.4): only its `PlanResult` does.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.counters import RunCounters
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import Inbound
from errand.domain.outcomes import PlanResult, RunOutcome

__all__ = ["Errand", "Phase", "SystemNote", "ThreadState"]


class SystemNote(BaseModel):
    """A code-to-planner note for this run (hold failed, slot taken). Used from M4."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hold_failed", "slot_taken", "slot_conflict", "over_budget"]
    text: str


class ThreadState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    user_id: str
    run_id: str
    inbound: Inbound | None = None
    errand: Errand | None = None  # one active errand per thread
    system_notes: list[SystemNote] = Field(default_factory=list)
    plan_result: PlanResult | None = None
    counters: RunCounters = Field(default_factory=RunCounters)
    outcome: RunOutcome | None = None
