"""Contract-suite fixtures: every test in tests/contract/ runs once per transport.

- `inprocess`: a WorldHost on a temp world.sqlite with a SimClock.
- `http`: one real errand-world subprocess per session, on port 0, reset to a fresh
  store per test through `/dev/scenario`.

Both give the test the same `Env`: a WorldClient-shaped transport, the separate
inspector, and the agent-side clock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from errand.clock import Clock, ClockMode, TrackingClock
from errand.tools.client import HttpTransport, InProcessTransport
from errand.tools.inspector import HttpInspector, InProcessInspector
from errand.tools.timeouts import ToolTimeouts
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig
from tests.contract.world_process import WorldProcess, start_world

Kind = Literal["inprocess", "http"]
Client = InProcessTransport | HttpTransport


@dataclass
class Env:
    kind: Kind
    inspector: InProcessInspector | HttpInspector
    host: WorldHost | None = None
    base_url: str = ""
    tracking: TrackingClock | None = None
    _clients: list[HttpTransport] = field(default_factory=list)

    async def load(
        self,
        scenario: ScenarioConfig | None = None,
        *,
        clock_mode: ClockMode = "sim",
        timeouts: ToolTimeouts | None = None,
    ) -> Client:
        scenario = scenario or ScenarioConfig(id="contract", seed=7)
        await self.inspector.load_scenario(scenario, clock_mode)
        return self.client(timeouts)

    def client(self, timeouts: ToolTimeouts | None = None) -> Client:
        if self.kind == "inprocess":
            assert self.host is not None
            return InProcessTransport(self.host, timeouts)
        client = HttpTransport(self.base_url, timeouts, clock=self.tracking)
        self._clients.append(client)
        return client

    @property
    def agent_clock(self) -> Clock:
        """What the agent would read `now` from."""
        if self.kind == "inprocess":
            assert self.host is not None
            return self.host.world.clock
        assert self.tracking is not None
        return self.tracking

    async def aclose(self) -> None:
        for client in self._clients:
            await client.__aexit__(None, None, None)
        if isinstance(self.inspector, HttpInspector):
            await self.inspector.__aexit__(None, None, None)
        if self.host is not None:
            self.host.close()


@pytest.fixture(scope="session")
def http_world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorldProcess]:
    world = start_world(tmp_path_factory.mktemp("http-world") / "world.sqlite", "--clock", "sim")
    yield world
    world.stop()


@pytest.fixture(params=["inprocess", "http"])
async def env(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Env]:
    kind: Kind = request.param
    if kind == "inprocess":
        host = WorldHost(tmp_path / "world.sqlite")
        made = Env(kind, InProcessInspector(host), host=host)
    else:
        world: WorldProcess = request.getfixturevalue("http_world")
        tracking = TrackingClock()
        made = Env(
            kind,
            HttpInspector(world.base_url, tracking),
            base_url=world.base_url,
            tracking=tracking,
        )
    try:
        yield made
    finally:
        await made.aclose()
