"""WorldInspector: eval and dev access to the world, kept apart from WorldClient.

The agent never gets one. Evals and the dev drawer use it to read the ground truth
(`op_log`, bookings, events), move the clock and load or reset a scenario. Over HTTP
it is the `/dev` routes, which only exist when the world runs with ERRAND_DEV=1.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol

import httpx

from errand.clock import CLOCK_HEADER, ClockMode, TrackingClock, from_ms
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig
from errand.world.store import BookingRow, EventRow, OperationRow, OpLogRow


class WorldInspector(Protocol):
    async def now(self) -> datetime: ...

    async def advance(self, seconds: float) -> datetime: ...

    async def tick(self) -> list[EventRow]: ...

    async def fire_provider_cancel(self, booking_id: str) -> EventRow: ...

    async def events(self) -> list[EventRow]: ...

    async def op_log(self) -> list[OpLogRow]: ...

    async def bookings(self) -> list[BookingRow]: ...

    async def operations(self) -> list[OperationRow]: ...

    async def load_scenario(
        self, scenario: ScenarioConfig, clock_mode: ClockMode = "sim"
    ) -> None: ...

    async def reset(self) -> None: ...


class InProcessInspector:
    def __init__(self, host: WorldHost) -> None:
        self._host = host

    async def now(self) -> datetime:
        return self._host.world.now()

    async def advance(self, seconds: float) -> datetime:
        return self._host.world.advance(seconds)

    async def tick(self) -> list[EventRow]:
        return self._host.world.tick()

    async def fire_provider_cancel(self, booking_id: str) -> EventRow:
        return self._host.world.fire_provider_cancel(booking_id)

    async def events(self) -> list[EventRow]:
        return self._host.world.events()

    async def op_log(self) -> list[OpLogRow]:
        return self._host.world.op_log()

    async def bookings(self) -> list[BookingRow]:
        return self._host.world.bookings()

    async def operations(self) -> list[OperationRow]:
        return self._host.world.operations()

    async def load_scenario(self, scenario: ScenarioConfig, clock_mode: ClockMode = "sim") -> None:
        self._host.load(scenario, clock_mode)

    async def reset(self) -> None:
        self._host.reset()


def row_to_json(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = dataclasses.asdict(row)
    return data


class HttpInspector:
    """The `/dev` routes. Every response refreshes `clock`, the agent-side view."""

    def __init__(
        self,
        base_url: str,
        clock: TrackingClock | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.clock = clock
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=30)

    async def __aenter__(self) -> HttpInspector:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        response = await self._http.request(method, path, json=body)
        response.raise_for_status()
        if self.clock is not None:
            self.clock.update_from_header(response.headers.get(CLOCK_HEADER))
        return response.json()

    async def now(self) -> datetime:
        data = await self._request("GET", "/dev/clock")
        return from_ms(int(data["now_ms"]))

    async def advance(self, seconds: float) -> datetime:
        data = await self._request("POST", "/dev/clock/advance", {"seconds": seconds})
        return from_ms(int(data["now_ms"]))

    async def tick(self) -> list[EventRow]:
        return [EventRow(**e) for e in await self._request("POST", "/dev/world/tick")]

    async def fire_provider_cancel(self, booking_id: str) -> EventRow:
        data = await self._request("POST", "/dev/world/events", {"booking_id": booking_id})
        return EventRow(**data)

    async def events(self) -> list[EventRow]:
        return [EventRow(**e) for e in await self._request("GET", "/dev/world/events")]

    async def op_log(self) -> list[OpLogRow]:
        return [OpLogRow(**r) for r in await self._request("GET", "/dev/op_log")]

    async def bookings(self) -> list[BookingRow]:
        return [BookingRow(**b) for b in await self._request("GET", "/dev/bookings")]

    async def operations(self) -> list[OperationRow]:
        return [OperationRow(**o) for o in await self._request("GET", "/dev/operations")]

    async def load_scenario(self, scenario: ScenarioConfig, clock_mode: ClockMode = "sim") -> None:
        body = {"scenario": scenario.model_dump(mode="json"), "clock_mode": clock_mode}
        await self._request("POST", "/dev/scenario", body)

    async def reset(self) -> None:
        await self._request("POST", "/dev/scenario", {})
