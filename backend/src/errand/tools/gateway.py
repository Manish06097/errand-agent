"""ToolGateway: every world call the agent makes goes through here (architecture 3, 15.1).

Two call paths:
- `call()`, the ordinary path: the read tools plus `hold_slot` and `get_booking_status`,
  per caller. It refuses `book`, `cancel` and `reschedule` for every caller.
- `_consequential()`, reachable only through `errand.tools.consequential`, which
  import-linter lets only `errand.execution.executor` import: `book`, `cancel` and
  `reschedule`, for `Caller.EXECUTOR` and nobody else.

Per call, in order:
1. caller allowlist: the planner reaches the three read tools and nothing else; a
   refusal emits `guard.planner_tool_rejected` and raises `PlannerToolRejected`
   without touching the world;
2. validation of the arguments against the wire contract (`ToolInvalid`);
3. the run's tool-call cap and deadline, checked before every attempt
   (`ToolBudgetExceeded`, nothing sent), except inside the booking critical section
   (`critical=True`: execute and verify), where a cap must never strand a booking
   half done (15.1); those calls still count;
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
from typing import Any, NoReturn

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
    # Code placing a hold and reading bookings for a proposal (prepare_proposal), never
    # the model: holds are not consequential, but they are not the planner's either.
    PROPOSER = "proposer"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    RECONCILER = "reconciler"
    WATCHER = "watcher"
    FOLLOWUP = "followup"
    RANKER = "ranker"


READ_TOOLS: frozenset[ToolName] = frozenset({"search_providers", "get_provider", "list_bookings"})
# Never reachable by the planner, whatever its tool list says (INV-11 checks the list).
CONSEQUENTIAL_TOOLS: frozenset[ToolName] = frozenset(
    {"book", "cancel", "reschedule", "hold_slot", "get_booking_status"}
)
# The side effects themselves: only `_consequential`, only for the executor.
SIDE_EFFECT_TOOLS: frozenset[ToolName] = frozenset({"book", "cancel", "reschedule"})
ALLOWED: Mapping[Caller, frozenset[ToolName]] = {
    Caller.PLANNER: READ_TOOLS,
    # get_provider: a reschedule's candidates come from the booked provider's own slots
    # (9.5, N1); only the slots are read, no review text is used.
    Caller.PROPOSER: frozenset({"hold_slot", "list_bookings", "get_provider"}),
    Caller.EXECUTOR: frozenset({"hold_slot", "list_bookings", "get_booking_status"}),
    Caller.VERIFIER: frozenset({"get_booking_status", "list_bookings"}),
    Caller.RECONCILER: frozenset({"get_booking_status", "list_bookings"}),
    Caller.WATCHER: frozenset({"list_bookings"}),
    Caller.FOLLOWUP: frozenset({"search_providers", "get_provider", "hold_slot"}),
    # Code fetching the top candidates' reviews for the reader before ranking (10.6).
    Caller.RANKER: frozenset({"get_provider"}),
}
assert not any(tools & SIDE_EFFECT_TOOLS for tools in ALLOWED.values()), (
    "book, cancel and reschedule never go through the ordinary call path"
)


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

    async def call(
        self,
        caller: Caller,
        tool: ToolName,
        args: Mapping[str, Any],
        *,
        critical: bool = False,
    ) -> WireModel:
        """Make one logical call (with its retry, for a read tool) and return the answer.

        Business answers (`not_found`, `invalid_args`, ...) are returned as typed
        results; transport failures raise.
        """
        if tool not in ALLOWED[caller]:
            self._refuse(caller, tool)
        return await self._call(caller, tool, args, critical=critical)

    async def _consequential(
        self, caller: Caller, tool: ToolName, args: Mapping[str, Any], *, attempt: int = 1
    ) -> WireModel:
        """book, cancel or reschedule. Only `errand.tools.consequential` calls this.

        One attempt, never retried here (execute and verify own those rules), inside
        the critical section: no cap refuses it and the run's time left does not
        shorten its timeout. `attempt` numbers the call in the tool.call record when the
        caller re-sends inside one attempt (cancel's 503 retry, 9.5).
        """
        if caller is not Caller.EXECUTOR or tool not in SIDE_EFFECT_TOOLS:
            self._refuse(caller, tool)
        return await self._call(caller, tool, args, critical=True, first_attempt=attempt)

    def _refuse(self, caller: Caller, tool: ToolName) -> NoReturn:
        if caller is Caller.PLANNER:
            self._tracer.emit(
                "guard.planner_tool_rejected",
                detail={"tool": tool, "caller": caller.value, "reason": "not_allowed"},
            )
        else:
            self._tracer.emit(
                "error",
                where="tools.gateway",
                exception_type="PlannerToolRejected",
                message=f"{caller.value} may not call {tool} on this path",
            )
        raise PlannerToolRejected(tool, f"{caller.value} may not call {tool}")

    async def _call(
        self,
        caller: Caller,
        tool: ToolName,
        args: Mapping[str, Any],
        *,
        critical: bool,
        first_attempt: int = 1,
    ) -> WireModel:
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
            time_left = None if critical else self._check_budget(tool)
            try:
                with self._tracer.opening("tool.call", tool):
                    return await self._attempt(
                        caller, tool, request, attempt + first_attempt - 1, time_left
                    )
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
