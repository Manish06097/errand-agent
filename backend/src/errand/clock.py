"""Time for everything in errand: one injected clock, never `datetime.now()` (ADR 0010).

- `SimClock` is simulated time. `sleep()` advances it instantly and yields to the event
  loop, so concurrent tasks still interleave. It does not persist itself; the owner
  (the world) passes `on_change` and stores `sim_now` wherever it wants.
- `SystemClock` is real time plus an offset. The demo world moves the offset with
  `/dev/clock/advance`.
- `TrackingClock` is the agent-side view of a world reached over HTTP. The world puts
  `X-World-Clock` on every response and the tracking clock refreshes from it.

Timestamps are integer epoch milliseconds in storage and ISO strings with +05:30 on
the wire. `to_ms` and `from_ms` are the only conversions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, Protocol

IST = timezone(timedelta(hours=5, minutes=30), "IST")
IST_OFFSET = timedelta(hours=5, minutes=30)

CLOCK_HEADER = "X-World-Clock"
AGENT_CLOCK_HEADER = "X-Agent-Clock"
ClockMode = Literal["sim", "system_offset"]


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt!r}: every errand time carries an offset")
    return round(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=IST)


def wall_now() -> datetime:
    """Real UTC time, never simulated and never offset.

    For records that must say when something really happened, such as the trace's
    `wall_ts` next to its clock-driven `ts`. Business logic uses an injected Clock.
    """
    return datetime.now(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SimClock:
    """Simulated time. Only `sleep`, `advance` and `set` move it."""

    mode: ClockMode = "sim"

    def __init__(
        self, start: datetime, on_change: Callable[[datetime], None] | None = None
    ) -> None:
        self._now_ms = to_ms(start)
        self._on_change = on_change

    def now(self) -> datetime:
        return from_ms(self._now_ms)

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("simulated time only moves forward")
        self._set_ms(self._now_ms + round(seconds * 1000))

    def set(self, dt: datetime) -> None:
        self._set_ms(to_ms(dt))

    def _set_ms(self, ms: int) -> None:
        self._now_ms = ms
        if self._on_change is not None:
            self._on_change(self.now())

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
        # Yield even for a zero sleep so a concurrent task (a same-key retry, say)
        # gets to run while this one is "waiting".
        await asyncio.sleep(0)


class SystemClock:
    """Real time plus an offset in milliseconds."""

    mode: ClockMode = "system_offset"

    def __init__(self, offset_ms: int = 0) -> None:
        self.offset_ms = offset_ms

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(IST) + timedelta(milliseconds=self.offset_ms)

    def now_ms(self) -> int:
        return to_ms(self.now())

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("the demo clock only moves forward")
        self.offset_ms += round(seconds * 1000)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def offset_to(target: datetime) -> int:
    """The SystemClock offset that makes `now()` read `target` right now."""
    return to_ms(target) - to_ms(datetime.now(UTC))


@dataclass(frozen=True)
class ClockReading:
    mode: ClockMode
    now_ms: int
    offset_ms: int


def encode_clock_header(mode: ClockMode, now_ms: int, offset_ms: int) -> str:
    return f"{mode}; now_ms={now_ms}; offset_ms={offset_ms}"


def parse_clock_header(value: str) -> ClockReading:
    parts = [p.strip() for p in value.split(";")]
    mode = parts[0]
    if mode not in ("sim", "system_offset"):
        raise ValueError(f"unknown clock mode in {CLOCK_HEADER}: {value!r}")
    fields: dict[str, int] = {}
    for part in parts[1:]:
        key, _, raw = part.partition("=")
        fields[key] = int(raw)
    return ClockReading(
        mode="sim" if mode == "sim" else "system_offset",
        now_ms=fields["now_ms"],
        offset_ms=fields["offset_ms"],
    )


class TrackingClock:
    """The agent's clock when the world is a separate process.

    In `system_offset` mode it is real time plus the world's offset, so it keeps
    ticking between responses. In `sim` mode it is the world's simulated time as of
    the last response, and `sleep` asks the world to advance through `advance_hook`.
    `HttpInspector` sets the hook to its `POST /dev/clock/advance` when it is given
    this clock and no hook is set yet; without one, a sim-mode `sleep` raises.
    """

    def __init__(self, advance_hook: Callable[[float], Awaitable[object]] | None = None) -> None:
        self._reading: ClockReading | None = None
        self.advance_hook = advance_hook

    @property
    def synced(self) -> bool:
        return self._reading is not None

    @property
    def mode(self) -> ClockMode:
        return self._require().mode

    def update(self, reading: ClockReading) -> None:
        self._reading = reading

    def update_from_header(self, value: str | None) -> None:
        if value:
            self.update(parse_clock_header(value))

    def now(self) -> datetime:
        reading = self._require()
        if reading.mode == "sim":
            return from_ms(reading.now_ms)
        return SystemClock(reading.offset_ms).now()

    async def sleep(self, seconds: float) -> None:
        if self._require().mode == "system_offset":
            await asyncio.sleep(max(0.0, seconds))
            return
        if self.advance_hook is None:
            raise RuntimeError("a simulated world clock can only be slept on through the world")
        await self.advance_hook(seconds)

    def _require(self) -> ClockReading:
        if self._reading is None:
            raise RuntimeError("TrackingClock has not seen the world clock yet")
        return self._reading


def reading_of(clock: Clock) -> ClockReading:
    """The agent clock as a client must sync to it (17.3): the mode, the agent's `now` in
    epoch ms, and, in `system_offset` mode, how far `now` is from real time. A client
    stamps `client_composed_at` as its own real clock plus that offset (or, in `sim`
    mode, as `now_ms` itself, since simulated time only moves when told to)."""
    now_ms = to_ms(clock.now())
    mode: ClockMode = "system_offset" if getattr(clock, "mode", "sim") == "system_offset" else "sim"
    offset_ms = now_ms - to_ms(datetime.now(UTC)) if mode == "system_offset" else 0
    return ClockReading(mode=mode, now_ms=now_ms, offset_ms=offset_ms)
