"""ToolGateway: every world call the agent makes goes through here (architecture 3, 15.1).

Per call, in order:
1. caller allowlist: the planner reaches the three read tools and nothing else; a
   refusal emits `guard.planner_tool_rejected` and raises `PlannerToolRejected`
   without touching the world;
2. validation of the arguments against the wire contract (`ToolInvalid`);
3. the run's tool-call cap and deadline, checked before every attempt
   (`ToolBudgetExceeded`, nothing sent);
4. the call, with its timeout from `ToolTimeouts` capped by the run time left;
5. a `tool.call` record per attempt: caller, args, result or error class, latency and
   the world's op_log seq. The tracer sanitizes both once.

Read tools get one retry after a backoff through `clock.sleep` on a timeout or a
transient failure, counted against the cap like any attempt; a second failure raises
`ToolUnavailable`. A malformed answer (`ToolUnexpected`) is not retried. Consequential
tools are never retried here: their retry rules belong to `execute` and `verify`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from errand.clock import Clock
from errand.domain.counters import RunCounters
from errand.tools.client import WorldClient, observe_call
from errand.tools.errors import (
    PlannerToolRejected,
    ToolBudgetExceeded,
    ToolError,
    ToolInvalid,
    ToolTimeout,
    ToolTransient,
    ToolUnavailable,
)
from errand.tools.timeouts import capped
from errand.trace.tracer import Tracer
from errand.world.contracts import (
    SPECS,
    BookIn,
    CancelIn,
    GetBookingStatusIn,
    GetProviderIn,
    HoldSlotIn,
    ListBookingsIn,
    RescheduleIn,
    SearchProvidersIn,
    ToolName,
    WireModel,
)


class Caller(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    RECONCILER = "reconciler"
    WATCHER = "watcher"
    FOLLOWUP = "followup"


READ_TOOLS: frozenset[ToolName] = frozenset({"search_providers", "get_provider", "list_bookings"})
# Never reachable by the planner, whatever its tool list says (INV-11 checks the list).
CONSEQUENTIAL_TOOLS: frozenset[ToolName] = frozenset(
    {"book", "cancel", "reschedule", "hold_slot", "get_booking_status"}
)
ALLOWED: Mapping[Caller, frozenset[ToolName]] = {
    Caller.PLANNER: READ_TOOLS,
    # The remaining callers are wired by the milestones that build them (M4 to M14).
    Caller.EXECUTOR: frozenset({"hold_slot", "book", "cancel", "reschedule"}),
    Caller.VERIFIER: frozenset({"get_booking_status", "list_bookings"}),
    Caller.RECONCILER: frozenset({"get_booking_status", "list_bookings"}),
    Caller.WATCHER: frozenset({"list_bookings"}),
    Caller.FOLLOWUP: frozenset({"search_providers", "get_provider", "hold_slot"}),
}


@dataclass(frozen=True)
class ToolLimits:
    max_tool_calls: int = 16
    read_retries: int = 1
    retry_backoff_s: float = 0.5


class ToolGateway:
    def __init__(
        self,
        client: WorldClient,
        *,
        tracer: Tracer,
        clock: Clock,
        counters: RunCounters,
        limits: ToolLimits | None = None,
    ) -> None:
        self._client = client
        self._tracer = tracer
        self._clock = clock
        self.counters = counters
        self.limits = limits or ToolLimits()

    async def call(self, caller: Caller, tool: ToolName, args: Mapping[str, Any]) -> WireModel:
        """Make one logical call (with its retry, for a read tool) and return the answer.

        Business answers (`not_found`, `invalid_args`, ...) are returned as typed
        results; transport failures raise.
        """
        if tool not in ALLOWED[caller]:
            self._tracer.emit(
                "guard.planner_tool_rejected" if caller is Caller.PLANNER else "error",
                **(
                    {"detail": {"tool": tool, "caller": caller.value, "reason": "not_allowed"}}
                    if caller is Caller.PLANNER
                    else {
                        "where": "tools.gateway",
                        "exception_type": "PlannerToolRejected",
                        "message": f"{caller.value} may not call {tool}",
                    }
                ),
            )
            raise PlannerToolRejected(tool, f"{caller.value} may not call {tool}")
        try:
            request = SPECS[tool].input.model_validate(dict(args))
        except ValidationError as exc:
            self._tracer.emit(
                "error",
                where="tools.gateway",
                exception_type="ToolInvalid",
                message=f"{tool}: {exc.errors()[0]['msg'] if exc.errors() else 'invalid'}",
            )
            raise ToolInvalid(tool, "arguments do not match the contract") from exc
        attempts = 1 + (self.limits.read_retries if tool in READ_TOOLS else 0)
        last: ToolError | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await self._clock.sleep(self.limits.retry_backoff_s)
            time_left = self._check_budget(tool)
            try:
                return await self._attempt(caller, tool, request, attempt, time_left)
            except (ToolTimeout, ToolTransient) as exc:
                last = exc
                if tool not in READ_TOOLS:
                    raise
        assert last is not None
        raise ToolUnavailable(tool, last)

    def _check_budget(self, tool: ToolName) -> float | None:
        if self.counters.tool_calls >= self.limits.max_tool_calls:
            raise ToolBudgetExceeded(tool, "tool_calls")
        time_left = self.counters.time_left_s(self._clock.now())
        if time_left is not None and time_left <= 0:
            raise ToolBudgetExceeded(tool, "deadline")
        return time_left

    async def _attempt(
        self,
        caller: Caller,
        tool: ToolName,
        request: WireModel,
        attempt: int,
        time_left: float | None,
    ) -> WireModel:
        self.counters.tool_calls += 1
        perf = self._tracer.perf
        args = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        key = args.get("idempotency_key")
        started = perf()
        with observe_call() as info:
            try:
                with capped(time_left):
                    result = await self._dispatch(tool, request)
            except ToolError as exc:
                self._tracer.tool_call(
                    tool=tool,
                    caller=caller.value,
                    args=args,
                    attempt=attempt,
                    idempotency_key=key,
                    error=type(exc).__name__,
                    latency_ms=(perf() - started) * 1000,
                    world_seq=info.world_seq,
                )
                raise
        self._tracer.tool_call(
            tool=tool,
            caller=caller.value,
            args=args,
            attempt=attempt,
            idempotency_key=key,
            result=result.model_dump(mode="json", by_alias=True, exclude_none=True),
            latency_ms=(perf() - started) * 1000,
            world_seq=info.world_seq,
        )
        return result

    async def _dispatch(self, tool: ToolName, request: WireModel) -> WireModel:
        c = self._client
        if isinstance(request, SearchProvidersIn):
            return await c.search_providers(request.service, request.area, request.date_range)
        if isinstance(request, GetProviderIn):
            return await c.get_provider(request.provider_id)
        if isinstance(request, ListBookingsIn):
            return await c.list_bookings(request.user_id)
        if isinstance(request, HoldSlotIn):
            return await c.hold_slot(request.provider_id, request.slot)
        if isinstance(request, BookIn):
            return await c.book(request.hold_id, request.idempotency_key)
        if isinstance(request, GetBookingStatusIn):
            return await c.get_booking_status(
                hold_id=request.hold_id, idempotency_key=request.idempotency_key
            )
        if isinstance(request, CancelIn):
            return await c.cancel(request.booking_id, request.idempotency_key)
        if isinstance(request, RescheduleIn):
            return await c.reschedule(request.booking_id, request.slot, request.idempotency_key)
        raise AssertionError(f"no dispatch for {tool}")
