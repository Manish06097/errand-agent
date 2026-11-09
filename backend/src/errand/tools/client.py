"""WorldClient: the eight tools and nothing else, over two transports.

Both transports send the request as JSON bytes and decode the response bytes with the
same strict decoder, so the in-process path cannot hand Python objects across and
drift from HTTP (ADR 0004). Business answers come back as typed results; transport
failures raise (`tools/errors.py`).

Dev and eval access (op_log, bookings, the clock, events, scenario load and reset) is
the separate inspector in `tools/inspector.py`. It is never part of WorldClient.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Protocol, TypeVar

import httpx
from pydantic import ValidationError

from errand.clock import CLOCK_HEADER, SimClock, TrackingClock
from errand.tools.errors import ToolTimeout, ToolTransient, ToolUnexpected
from errand.tools.timeouts import ToolTimeouts, effective_timeout
from errand.world.contracts import (
    CLIENT_TIMEOUT_HEADER,
    WORLD_SEQ_HEADER,
    Area,
    BookError,
    BookIn,
    BookingStatusError,
    BookingStatusOut,
    BookOut,
    CancelError,
    CancelIn,
    CancelOut,
    DateRange,
    GetBookingStatusIn,
    GetProviderError,
    GetProviderIn,
    GetProviderOut,
    HoldSlotError,
    HoldSlotIn,
    HoldSlotOut,
    ListBookingsError,
    ListBookingsIn,
    ListBookingsOut,
    RescheduleError,
    RescheduleIn,
    RescheduleOut,
    SearchProvidersError,
    SearchProvidersIn,
    SearchProvidersOut,
    Service,
    Slot,
    ToolName,
    WireModel,
    encode,
)
from errand.world.engine import WorldHost

OkT = TypeVar("OkT", bound=WireModel)
ErrT = TypeVar("ErrT", bound=WireModel)


@dataclass
class CallInfo:
    """What a transport learned about one call besides its answer."""

    world_seq: int | None = None


_call_info: ContextVar[CallInfo | None] = ContextVar("errand_world_call_info", default=None)


@contextmanager
def observe_call() -> Iterator[CallInfo]:
    """Collect the world's op_log seq of the call made inside the block, when known."""
    info = CallInfo()
    token = _call_info.set(info)
    try:
        yield info
    finally:
        _call_info.reset(token)


def _note_seq(seq: int | None) -> None:
    info = _call_info.get()
    if info is not None:
        info.world_seq = seq


class WorldClient(Protocol):
    async def search_providers(
        self, service: Service, area: Area, date_range: DateRange
    ) -> SearchProvidersOut | SearchProvidersError: ...

    async def get_provider(self, provider_id: str) -> GetProviderOut | GetProviderError: ...

    async def hold_slot(self, provider_id: str, slot: Slot) -> HoldSlotOut | HoldSlotError: ...

    async def book(self, hold_id: str, idempotency_key: str) -> BookOut | BookError: ...

    async def get_booking_status(
        self, *, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusOut | BookingStatusError: ...

    async def cancel(self, booking_id: str, idempotency_key: str) -> CancelOut | CancelError: ...

    async def reschedule(
        self, booking_id: str, slot: Slot, idempotency_key: str
    ) -> RescheduleOut | RescheduleError: ...

    async def list_bookings(self, user_id: str) -> ListBookingsOut | ListBookingsError: ...


def decode(tool: ToolName, status: int, body: bytes, ok: type[OkT], err: type[ErrT]) -> OkT | ErrT:
    """The one strict decoder both transports use."""
    if status >= 500:
        raise ToolTransient(tool, status, body)
    if status != 200:
        raise ToolUnexpected(tool, body, f"HTTP {status}", status)
    try:
        return ok.model_validate_json(body)
    except ValidationError as ok_error:
        try:
            return err.model_validate_json(body)
        except ValidationError:
            reason = ok_error.errors()[0]["msg"] if ok_error.errors() else "invalid"
            raise ToolUnexpected(tool, body, reason, status) from ok_error


class _Transport:
    """The eight tools, written once over `_send`. Subclasses only move bytes."""

    def __init__(self, timeouts: ToolTimeouts | None) -> None:
        self._timeouts = timeouts or ToolTimeouts()

    async def _send(self, tool: ToolName, payload: bytes) -> tuple[int, bytes]:
        raise NotImplementedError

    async def _call(
        self, tool: ToolName, request: WireModel, ok: type[OkT], err: type[ErrT]
    ) -> OkT | ErrT:
        status, body = await self._send(tool, encode(request))
        return decode(tool, status, body, ok, err)

    async def search_providers(
        self, service: Service, area: Area, date_range: DateRange
    ) -> SearchProvidersOut | SearchProvidersError:
        request = SearchProvidersIn(service=service, area=area, date_range=date_range)
        return await self._call(
            "search_providers", request, SearchProvidersOut, SearchProvidersError
        )

    async def get_provider(self, provider_id: str) -> GetProviderOut | GetProviderError:
        request = GetProviderIn(provider_id=provider_id)
        return await self._call("get_provider", request, GetProviderOut, GetProviderError)

    async def hold_slot(self, provider_id: str, slot: Slot) -> HoldSlotOut | HoldSlotError:
        request = HoldSlotIn(provider_id=provider_id, slot=slot)
        return await self._call("hold_slot", request, HoldSlotOut, HoldSlotError)

    async def book(self, hold_id: str, idempotency_key: str) -> BookOut | BookError:
        request = BookIn(hold_id=hold_id, idempotency_key=idempotency_key)
        return await self._call("book", request, BookOut, BookError)

    async def get_booking_status(
        self, *, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusOut | BookingStatusError:
        request = GetBookingStatusIn(hold_id=hold_id, idempotency_key=idempotency_key)
        return await self._call("get_booking_status", request, BookingStatusOut, BookingStatusError)

    async def cancel(self, booking_id: str, idempotency_key: str) -> CancelOut | CancelError:
        request = CancelIn(booking_id=booking_id, idempotency_key=idempotency_key)
        return await self._call("cancel", request, CancelOut, CancelError)

    async def reschedule(
        self, booking_id: str, slot: Slot, idempotency_key: str
    ) -> RescheduleOut | RescheduleError:
        request = RescheduleIn(booking_id=booking_id, slot=slot, idempotency_key=idempotency_key)
        return await self._call("reschedule", request, RescheduleOut, RescheduleError)

    async def list_bookings(self, user_id: str) -> ListBookingsOut | ListBookingsError:
        request = ListBookingsIn(user_id=user_id)
        return await self._call("list_bookings", request, ListBookingsOut, ListBookingsError)


class InProcessTransport(_Transport):
    """The world in this process, still spoken to in JSON bytes.

    Timeouts follow the world clock: a response that took the tool's timeout or
    longer in world time is a timeout, and a `wire_timeout` answer advances the clock
    to the deadline and raises, with no real wait under a SimClock.
    """

    def __init__(self, host: WorldHost, timeouts: ToolTimeouts | None = None) -> None:
        super().__init__(timeouts)
        self._host = host

    async def _send(self, tool: ToolName, payload: bytes) -> tuple[int, bytes]:
        world = self._host.world
        clock = world.clock
        timeout = effective_timeout(self._timeouts, tool)
        started = clock.now()
        response = await world.handle(tool, payload)
        _note_seq(response.seq)
        elapsed = (clock.now() - started).total_seconds()
        if response.wire_timeout:
            await clock.sleep(max(0.0, timeout - elapsed))
            raise ToolTimeout(tool, timeout)
        if elapsed >= timeout:
            # The world answered after the client gave up. In simulated time the client
            # was back at `started + timeout`, not when the world finished: otherwise a
            # read capped by the run deadline would run past it (BUG-N3-1).
            if isinstance(clock, SimClock):
                clock.set(started + timedelta(seconds=timeout))
            raise ToolTimeout(tool, timeout)
        return response.status, response.body


class HttpTransport(_Transport):
    """The world over HTTP. Use as an async context manager to close the connection pool.

    If a `TrackingClock` is given, it is refreshed from every response's clock header.
    """

    def __init__(
        self,
        base_url: str,
        timeouts: ToolTimeouts | None = None,
        clock: TrackingClock | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeouts)
        self._clock = clock
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url)

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _send(self, tool: ToolName, payload: bytes) -> tuple[int, bytes]:
        timeout = effective_timeout(self._timeouts, tool)
        headers = {
            "content-type": "application/json",
            CLIENT_TIMEOUT_HEADER: str(round(timeout * 1000)),
        }
        try:
            # httpx's timeout is per phase; asyncio.timeout bounds the whole call.
            async with asyncio.timeout(timeout):
                response = await self._http.post(
                    f"/tools/{tool}", content=payload, headers=headers, timeout=timeout
                )
        except (TimeoutError, httpx.TimeoutException):
            raise ToolTimeout(tool, timeout) from None
        except httpx.TransportError as exc:
            raise ToolTransient(tool, None, b"", type(exc).__name__) from exc
        if self._clock is not None:
            self._clock.update_from_header(response.headers.get(CLOCK_HEADER))
        seq = response.headers.get(WORLD_SEQ_HEADER)
        _note_seq(int(seq) if seq and seq.isdigit() else None)
        return response.status_code, response.content
