"""How the agent process reaches the world: a client and the clock that goes with it.

This is the only place outside `errand.world` that builds a world host. The API and
the agent get a `WorldAccess` and never import the engine, store, server or catalog
(an import-linter contract pins that).

- In process (evals, tests): the engine's own clock is the agent's clock, so a
  simulated `sleep` in the agent moves the world too.
- Over HTTP (`make dev`): a `TrackingClock` refreshed from every world response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from errand.clock import CLOCK_HEADER, Clock, ClockMode, TrackingClock
from errand.tools.client import HttpTransport, InProcessTransport, WorldClient
from errand.tools.timeouts import ToolTimeouts
from errand.world.engine import WorldHost


class HostClock:
    """The current world's clock. A scenario load replaces the world, so look it up
    on every call rather than holding the old clock."""

    def __init__(self, host: WorldHost) -> None:
        self._host = host

    @property
    def mode(self) -> ClockMode:
        return self._host.world.mode

    def now(self) -> datetime:
        return self._host.world.clock.now()

    async def sleep(self, seconds: float) -> None:
        await self._host.world.clock.sleep(seconds)


async def _noop() -> None:
    return None


@dataclass
class WorldAccess:
    client: WorldClient
    clock: Clock
    aclose: Callable[[], Awaitable[None]] = _noop


def in_process(host: WorldHost, timeouts: ToolTimeouts | None = None) -> WorldAccess:
    return WorldAccess(client=InProcessTransport(host, timeouts), clock=HostClock(host))


def open_in_process(path: Path, timeouts: ToolTimeouts | None = None) -> WorldAccess:
    """A world already seeded at `path` (by errand-world or an eval runner)."""
    host = WorldHost(path)
    if host.reopen() is None:
        raise FileNotFoundError(f"{path} holds no world; start errand-world once to seed it")

    async def close() -> None:
        host.close()

    access = in_process(host, timeouts)
    access.aclose = close
    return access


async def over_http(
    base_url: str, timeouts: ToolTimeouts | None = None, *, wait_s: float = 30.0
) -> WorldAccess:
    """The world at `base_url`, waiting up to `wait_s` for it to come up (make dev
    starts the world and the API together)."""
    clock = TrackingClock()
    http = httpx.AsyncClient(base_url=base_url)
    # /health carries the clock header, so the clock is in sync before the first run.
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        try:
            response = await http.get("/health")
            response.raise_for_status()
            break
        except httpx.HTTPError:
            if asyncio.get_running_loop().time() >= deadline:
                await http.aclose()
                raise
            await asyncio.sleep(0.25)
    clock.update_from_header(response.headers.get(CLOCK_HEADER))
    if not clock.synced:
        await http.aclose()
        raise RuntimeError(f"the world at {base_url} sent no {CLOCK_HEADER}")
    if clock.mode == "sim":
        # A simulated world moves only when told to: the agent's `clock.sleep` (a verify
        # backoff, a cancel's retry delay) advances it through /dev/clock/advance, which
        # the world serves with ERRAND_DEV=1 (the restart harness runs it that way).

        async def advance(seconds: float) -> None:
            moved = await http.post("/dev/clock/advance", json={"seconds": seconds})
            moved.raise_for_status()
            clock.update_from_header(moved.headers.get(CLOCK_HEADER))

        clock.advance_hook = advance
    transport = HttpTransport(base_url, timeouts, clock=clock, http=http)
    return WorldAccess(client=transport, clock=clock, aclose=http.aclose)
