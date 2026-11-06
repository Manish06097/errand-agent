"""RunDeps: everything a node needs for one run, passed as LangGraph's run context.

The context is never checkpointed (unlike state), so live objects (the tracer, both
gateways, the database) belong here. The RunManager builds one per run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import tzinfo

from errand.clock import Clock
from errand.config import LimitSettings
from errand.domain.counters import RunCounters
from errand.llm.gateway import LLMGateway
from errand.store.agent_db import AgentDB
from errand.tools.gateway import ToolGateway
from errand.trace.tracer import Tracer

TokenSink = Callable[[str], Awaitable[None] | None]


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
    on_token: TokenSink | None = None
