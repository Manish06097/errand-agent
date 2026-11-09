"""`errand chat` end to end: a real world and a real API in scripted mode, and the CLI as
a subprocess (M7). It runs S01's flow with nothing but the terminal client: the same
routes, the same agent clock, the same server-side guards as the browser."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from errand.cli.chat import AgentClock
from errand.tools.inspector import HttpInspector
from errand.world.scenario import ScenarioConfig
from evals.dsl import load_scenario
from tests.contract.world_process import WorldProcess, start_world
from tests.hygiene.repo import REPO_ROOT

BACKEND = REPO_ROOT / "backend"
OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under 1500."


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Stack:
    def __init__(self, world: WorldProcess, api: subprocess.Popen[str], url: str) -> None:
        self.world = world
        self.api = api
        self.url = url


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    world = start_world(
        tmp_path / "world.sqlite"
    )  # system_offset: the agent clock is off real time
    scenario = load_scenario(BACKEND / "evals" / "scenarios" / "S01_happy_path_plumber.yaml")
    inspector = HttpInspector(world.base_url)

    async def load(config: ScenarioConfig) -> None:
        await inspector.load_scenario(config, "system_offset")
        await inspector.__aexit__(None, None, None)

    import asyncio

    asyncio.run(load(scenario.world_config()))
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env.update(
        ERRAND_WORLD_URL=world.base_url,
        ERRAND_API_PORT=str(port),
        ERRAND_DATA_DIR=str(tmp_path / "data"),
        ERRAND_LANGFUSE="off",
        PYTHONPATH=str(BACKEND),
        PYTHONUNBUFFERED="1",
    )
    api = subprocess.Popen(
        [sys.executable, "-c", "from errand.api.app import main; main()"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        api.kill()
        raise RuntimeError("the API did not come up")
    try:
        yield Stack(world, api, url)
    finally:
        api.terminate()
        try:
            api.wait(timeout=15)
        except subprocess.TimeoutExpired:
            api.kill()
        world.stop()


def run_chat(stack: Stack, lines: list[str]) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "errand.cli.chat", "chat", "--api", stack.url],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(BACKEND / "src")},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def created_bookings(stack: Stack) -> list[dict[str, object]]:
    rows = httpx.get(f"{stack.world.base_url}/dev/bookings", timeout=10).json()
    return [r for r in rows if r["created_by_key"] is not None]


@pytest.mark.slow
def test_s01_by_numbered_approve(stack: Stack) -> None:
    out = run_chat(stack, [OPENER, "1"])
    assert "[1] Book for" in out and "[2] Decline" in out
    assert "[verified]" in out
    assert len(created_bookings(stack)) == 1


@pytest.mark.slow
def test_s01_by_typing_book_it_on_the_synced_clock(stack: Stack) -> None:
    """The typed grant needs a stamp on the agent clock, which on this world is hours off
    real time: a client that used time.time() would never be granted."""
    out = run_chat(stack, [OPENER, "book it"])
    assert "[verified]" in out, out
    assert len(created_bookings(stack)) == 1


def test_the_agent_clock_runs_on_a_monotonic_clock_from_the_last_reading() -> None:
    now = [1000.0]
    clock = AgentClock(monotonic=lambda: now[0])
    clock.sync_header("system_offset; now_ms=50000000; offset_ms=52416000")
    now[0] += 2.5
    assert clock.now_ms() == 50_002_500
    clock.sync_header("sim; now_ms=7000; offset_ms=0")
    now[0] += 99
    assert clock.now_ms() == 7_000  # a simulated clock only moves when told to
    clock.sync_header("sim; now_ms=6500; offset_ms=0")
    assert clock.now_ms() == 7_000  # jitter between responses never runs it backwards
    clock.sync_header("sim; now_ms=1000; offset_ms=0")
    assert clock.now_ms() == 1_000  # a real jump back (a world reload) is the new truth
