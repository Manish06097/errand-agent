"""The HTTP world as a process: one process only, and a restart mid-latency.

These tests start their own errand-world processes, so they are not parametrised
over transports.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from errand.tools.client import HttpTransport
from errand.tools.errors import ToolTimeout
from errand.tools.inspector import HttpInspector
from errand.tools.timeouts import ToolTimeouts
from errand.world.contracts import BookingStatusOut, BookOut, GetProviderOut, HoldSlotOut
from errand.world.scenario import (
    BookConfig,
    CommitLatency,
    FaultsConfig,
    ScenarioConfig,
    WorldConfig,
)
from tests.contract.helpers import rows
from tests.contract.world_process import start_world, world_env

pytestmark = pytest.mark.slow  # subprocesses or real servers: serial step of make check


def test_refuses_more_than_one_worker(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "errand.world.server",
            "--workers",
            "2",
            "--db",
            str(tmp_path / "w.sqlite"),
        ],
        env=world_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "exactly one process" in result.stderr


def test_a_second_process_on_the_same_store_refuses_to_start(tmp_path: Path) -> None:
    db = tmp_path / "w.sqlite"
    first = start_world(db, "--clock", "sim")
    try:
        with pytest.raises(RuntimeError, match="exactly one process"):
            start_world(db, "--clock", "sim")
        assert httpx.get(f"{first.base_url}/health").status_code == 200
    finally:
        first.stop()


def test_dev_routes_need_errand_dev(tmp_path: Path) -> None:
    env = world_env()
    del env["ERRAND_DEV"]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "errand.world.server",
            "--port",
            "0",
            "--db",
            str(tmp_path / "w.sqlite"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        base = line.strip().rsplit(" ", 1)[-1]
        assert httpx.get(f"{base}/health").status_code == 200
        assert httpx.get(f"{base}/dev/op_log").status_code == 404
        assert httpx.post(f"{base}/dev/clock/advance", json={"seconds": 60}).status_code == 404
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_world_restart_mid_latency_marks_the_key_not_applied(tmp_path: Path) -> None:
    db = tmp_path / "w.sqlite"
    world = start_world(db, "--clock", "sim")
    scenario = ScenarioConfig(
        id="restart",
        seed=4,
        world=WorldConfig(book=BookConfig(forced=["success"])),
        faults=FaultsConfig(commit_latency_ms=CommitLatency(book=1500)),
    )
    try:
        async with HttpInspector(world.base_url) as inspector:
            await inspector.load_scenario(scenario)
        async with HttpTransport(world.base_url, ToolTimeouts(book=0.3)) as client:
            details = await client.get_provider("P3")
            assert isinstance(details, GetProviderOut)
            held = await client.hold_slot("P3", details.available_slots[0])
            assert isinstance(held, HoldSlotOut)
            with pytest.raises(ToolTimeout):
                await client.book(held.hold_id, "k-restart")
    finally:
        world.stop(kill=True)  # dies while the claimed book is still sleeping

    world = start_world(db, "--clock", "sim")
    try:
        async with HttpInspector(world.base_url) as inspector:
            (row,) = rows(await inspector.op_log(), "book", "k-restart")
            assert row.outcome == "interrupted"
            assert await inspector.bookings() != []
            assert not [b for b in await inspector.bookings() if b.created_by_key == "k-restart"]
            async with HttpTransport(world.base_url, ToolTimeouts(book=5)) as client:
                status = await client.get_booking_status(idempotency_key="k-restart")
                assert status == BookingStatusOut(status="not_found")
                # The key was not_applied, so the same-key retry is evaluated afresh.
                retry = await client.book(held.hold_id, "k-restart")
                assert isinstance(retry, BookOut)
            log = rows(await inspector.op_log(), "book", "k-restart")
            assert [(r.outcome, r.attempt) for r in log] == [("interrupted", None), ("success", 1)]
    finally:
        world.stop()
