"""RunDeps: everything a node needs for one run, passed as LangGraph's run context.

The context is never checkpointed (unlike state), so live objects (the tracer, both
gateways, the review reader, the database) belong here. The RunManager builds one per run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Protocol

from errand.approvals.service import ApprovalPolicy
from errand.clock import Clock
from errand.config import ApprovalSettings, BookingSettings, LimitSettings
from errand.domain.counters import RunCounters
from errand.llm.gateway import LLMGateway
from errand.quarantine.reader import ReviewReader
from errand.store.agent_db import AgentDB
from errand.tools.gateway import ToolGateway
from errand.tools.timeouts import ToolTimeouts
from errand.trace.tracer import Tracer

TokenSink = Callable[[str], Awaitable[None] | None]


class HeldLock(Protocol):
    """The per-user consequential lock as one run holds it (agent/locks.py)."""

    epoch: int

    async def release(self) -> None: ...


class ConsequentialLocks(Protocol):
    async def acquire(self, user_id: str) -> HeldLock: ...


@dataclass
class RunDeps:
    run_id: str
    conversation_id: str
    user_id: str
    tracer: Tracer
    llm: LLMGateway
    tools: ToolGateway
    counters: RunCounters
    db: AgentDB
    clock: Clock
    tz: tzinfo
    limits: LimitSettings
    # The quarantined review reader; it never gets `on_token`, so nothing it returns can
    # reach the SSE stream as a token.
    reader: ReviewReader
    on_token: TokenSink | None = None
    approval_settings: ApprovalSettings = field(default_factory=ApprovalSettings)
    booking_settings: BookingSettings = field(default_factory=BookingSettings)
    timeouts: ToolTimeouts = field(default_factory=ToolTimeouts)
    consequential: ConsequentialLocks | None = None
    # The consequential lock this run holds, if any: taken in execute, released after
    # verify (or by the RunManager when the run ends, whatever happened).
    held: list[HeldLock] = field(default_factory=list)

    @property
    def approval_policy(self) -> ApprovalPolicy:
        s = self.approval_settings
        return ApprovalPolicy(
            ttl_ms=round(s.ttl_s * 1000), grace_ms=round(s.execute_grace_s * 1000)
        )

    async def release_held(self) -> None:
        while self.held:
            await self.held.pop().release()
