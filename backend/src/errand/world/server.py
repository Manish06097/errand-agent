"""errand-world: the mock provider world over HTTP.

- `POST /tools/{tool}` takes the request JSON and returns the world's bytes unchanged.
  Business errors are 200 with the error body; the cancel 503 is a real HTTP 503.
- A `timeout_booked` or `timeout_not_booked` answer is a real network timeout: the
  world has already committed (or not), then holds the connection open for the
  client's own timeout (from `X-Errand-Client-Timeout-Ms`) plus a margin.
- `/dev/*` exists only with ERRAND_DEV=1: the clock, events, scenario load and reset,
  `op_log` and bookings.
- Every response carries `X-World-Clock`, which agent-side clocks track.
- One process only. The engine's per-key latches live in memory, so the server takes
  an exclusive lock on `<db>.lock` at startup and refuses `--workers` above 1.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Any, Literal

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from errand.clock import CLOCK_HEADER, ClockMode, SimClock, to_ms
from errand.world.contracts import (
    CLIENT_TIMEOUT_HEADER,
    SERVICE_UNAVAILABLE,
    WORLD_SEQ_HEADER,
    encode,
)
from errand.world.engine import ClockPacing, Pacing, UnknownTool, World, WorldClosed, WorldHost
from errand.world.scenario import ScenarioConfig, WorldConfig, parse_duration
from errand.world.store import BookingRow, EventRow, OperationRow, OpLogRow

SERVICE = "errand-world"
# Only used when a caller (curl, say) sends no timeout header; HttpTransport always
# sends the value from tools/timeouts.py.
FALLBACK_CLIENT_TIMEOUT_S = 8.0


class ServeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_WORLD_")

    host: str = "127.0.0.1"
    port: int = 8765
    db: Path = Path("data/world.sqlite")
    clock: ClockMode = "system_offset"
    workers: int = 1
    reset: bool = False
    timeout_margin_s: float = 2.0


class DevFlag(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_")

    dev: bool = False


def default_scenario() -> ScenarioConfig:
    """What `make dev` serves until a scenario is loaded through /dev/scenario."""
    return ScenarioConfig(
        id="dev_default", seed=1, world=WorldConfig(latency_ms={"default": 120, "book": 900})
    )


class HttpPacing:
    """Delays as an HTTP client experiences them.

    Op latency follows the world clock. A late commit is always a real wait, so the
    client's real timeout can fire before the commit; under a SimClock the simulated
    time then moves on by the same amount before the commit lands.
    """

    def __init__(self, world: World) -> None:
        self._clock = world.clock
        self._inner = ClockPacing(world.clock)

    async def latency(self, seconds: float) -> None:
        await self._inner.latency(seconds)

    async def late_commit(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if isinstance(self._clock, SimClock):
            self._clock.advance(seconds)


def _http_pacing(world: World) -> Pacing:
    return HttpPacing(world)


class _Advance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: float | None = None
    by: str | None = None

    def total_seconds(self) -> float:
        if (self.seconds is None) == (self.by is None):
            raise ValueError("give exactly one of seconds or by")
        if self.by is not None:
            return parse_duration(self.by).total_seconds()
        assert self.seconds is not None
        return self.seconds


class _Load(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: dict[str, Any] | None = None
    clock_mode: ClockMode | None = None


class _Fire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["provider_cancelled"] = "provider_cancelled"
    booking_id: str


def _event_json(e: EventRow) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "type": e.type,
        "target": e.target,
        "fire_at": e.fire_at,
        "fired_at": e.fired_at,
    }


def _op_json(r: OpLogRow) -> dict[str, Any]:
    return {
        "seq": r.seq,
        "sim_ts": r.sim_ts,
        "op": r.op,
        "idempotency_key": r.idempotency_key,
        "args_json": r.args_json,
        "outcome": r.outcome,
        "http_status": r.http_status,
        "response_json": r.response_json,
        "draw": r.draw,
        "attempt": r.attempt,
        "done_ts": r.done_ts,
        "done_seq": r.done_seq,
    }


def _booking_json(b: BookingRow) -> dict[str, Any]:
    return {
        "booking_id": b.booking_id,
        "user_id": b.user_id,
        "provider_id": b.provider_id,
        "service": b.service,
        "slot_start": b.slot_start,
        "slot_end": b.slot_end,
        "price_inr": b.price_inr,
        "status": b.status,
        "hold_id": b.hold_id,
        "created_by_key": b.created_by_key,
        "created_at": b.created_at,
    }


def _operation_json(o: OperationRow) -> dict[str, Any]:
    return {
        "idempotency_key": o.idempotency_key,
        "op": o.op,
        "request_hash": o.request_hash,
        "outcome": o.outcome,
        "response_json": o.response_json,
        "booking_id": o.booking_id,
        "calls": o.calls,
        "attempts": o.attempts,
    }


class _SingleProcessLock:
    def __init__(self, db: Path) -> None:
        self.path = Path(f"{db}.lock")
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise RuntimeError(
                f"another errand-world process is serving {self.path.with_suffix('')}; "
                "the world runs as exactly one process"
            ) from None
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def create_app(settings: ServeSettings | None = None, dev: bool | None = None) -> FastAPI:
    settings = settings or ServeSettings()
    dev_routes = DevFlag().dev if dev is None else dev
    host = WorldHost(settings.db, pacing=_http_pacing)
    lock = _SingleProcessLock(settings.db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock.acquire()
        try:
            if settings.reset or host.reopen() is None:
                host.load(default_scenario(), settings.clock)
            yield
        finally:
            host.close()
            lock.release()

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.state.host = host

    @app.middleware("http")
    async def clock_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if host.loaded:
            response.headers[CLOCK_HEADER] = host.world.clock_header()
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE}

    @app.post("/tools/{tool}")
    async def call_tool(tool: str, request: Request) -> Response:
        payload = await request.body()
        world = host.world
        started = world.now_ms()
        try:
            answer = await world.handle(tool, payload)
        except UnknownTool:
            return JSONResponse({"error": "unknown_tool"}, status_code=404)
        except WorldClosed:
            return Response(encode(SERVICE_UNAVAILABLE), 503, media_type="application/json")
        if answer.wire_timeout:
            timeout = _client_timeout(request)
            if isinstance(world.clock, SimClock):
                # Keep world time in step with the in-process transport, which
                # advances to the client's deadline when it raises.
                elapsed = (world.now_ms() - started) / 1000
                world.clock.advance(max(0.0, timeout - elapsed))
            await asyncio.sleep(timeout + settings.timeout_margin_s)
            return Response(b"", 504)
        headers = {} if answer.seq is None else {WORLD_SEQ_HEADER: str(answer.seq)}
        return Response(answer.body, answer.status, media_type="application/json", headers=headers)

    if dev_routes:
        _add_dev_routes(app, host, settings)
    return app


def _client_timeout(request: Request) -> float:
    raw = request.headers.get(CLIENT_TIMEOUT_HEADER)
    try:
        return int(raw) / 1000 if raw else FALLBACK_CLIENT_TIMEOUT_S
    except ValueError:
        return FALLBACK_CLIENT_TIMEOUT_S


def _add_dev_routes(app: FastAPI, host: WorldHost, settings: ServeSettings) -> None:
    def clock_json() -> dict[str, Any]:
        world = host.world
        now = world.now()
        offset = getattr(world.clock, "offset_ms", 0)
        return {
            "mode": world.mode,
            "now": now.isoformat(),
            "now_ms": to_ms(now),
            "offset_ms": offset,
        }

    @app.get("/dev/clock")
    async def get_clock() -> dict[str, Any]:
        return clock_json()

    @app.post("/dev/clock/advance")
    async def advance(body: _Advance) -> Any:
        try:
            seconds = body.total_seconds()
            host.world.advance(seconds)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_args", "reason": str(exc)}, status_code=400)
        return clock_json()

    @app.get("/dev/world/events")
    async def list_events() -> list[dict[str, Any]]:
        return [_event_json(e) for e in host.world.events()]

    @app.post("/dev/world/events")
    async def fire_event(body: _Fire) -> dict[str, Any]:
        return _event_json(host.world.fire_provider_cancel(body.booking_id))

    @app.post("/dev/world/tick")
    async def tick() -> list[dict[str, Any]]:
        return [_event_json(e) for e in host.world.tick()]

    @app.get("/dev/scenario")
    async def get_scenario() -> dict[str, Any]:
        world = host.world
        return {
            "scenario": world.scenario.model_dump(mode="json"),
            "clock_mode": world.mode,
            "faults": world.scenario.fault_names(),
        }

    @app.post("/dev/scenario")
    async def load_scenario(body: _Load) -> Any:
        """Load a scenario into a fresh store; an empty body resets the current one."""
        try:
            if body.scenario is None:
                world = host.reset(body.clock_mode)
            else:
                scenario = ScenarioConfig.model_validate(body.scenario)
                world = host.load(scenario, body.clock_mode or settings.clock)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_args", "reason": str(exc)}, status_code=400)
        return {"scenario_id": world.scenario.id, "seed": world.scenario.seed, **clock_json()}

    @app.get("/dev/op_log")
    async def op_log() -> list[dict[str, Any]]:
        return [_op_json(r) for r in host.world.op_log()]

    @app.get("/dev/bookings")
    async def bookings() -> list[dict[str, Any]]:
        return [_booking_json(b) for b in host.world.bookings()]

    @app.get("/dev/operations")
    async def operations() -> list[dict[str, Any]]:
        return [_operation_json(o) for o in host.world.operations()]


app = create_app()


class _Server(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started and sockets:
            host, port = sockets[0].getsockname()[:2]
            # Tests start the world on port 0 and read the real port from this line.
            print(f"errand-world listening on http://{host}:{port}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="errand-world")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--clock", choices=["sim", "system_offset"])
    parser.add_argument("--workers", type=int)
    parser.add_argument("--reset", action="store_true", default=None)
    args = parser.parse_args(argv)
    overrides = {k: v for k, v in vars(args).items() if v is not None}
    settings = ServeSettings(**overrides)
    if settings.workers != 1:
        print(
            f"errand-world: refusing to start {settings.workers} workers. The world keeps "
            "its per-key idempotency latches in memory, so it runs as exactly one process.",
            file=sys.stderr,
        )
        sys.exit(2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((settings.host, settings.port))
    config = uvicorn.Config(create_app(settings), log_level="warning", timeout_graceful_shutdown=1)
    _Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
