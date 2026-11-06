"""Both transports run the same engine and the same JSON path (ADR 0004).

The same scenario and the same calls must produce byte-identical responses in
process and over HTTP. A transport that handed Python objects across, or serialised
datetimes its own way, fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errand.tools.client import HttpTransport, InProcessTransport
from errand.tools.inspector import HttpInspector
from errand.world.contracts import ToolName
from errand.world.engine import WorldHost
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from tests.contract.world_process import WorldProcess

SCENARIO = ScenarioConfig(
    id="parity", seed=13, world=WorldConfig(book=BookConfig(forced=["success", "slot_taken"]))
)


async def script(client: InProcessTransport | HttpTransport) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []

    async def call(tool: ToolName, args: dict[str, Any]) -> Any:
        status, body = await client._send(tool, json.dumps(args).encode())
        out.append((status, body))
        return json.loads(body) if body else None

    week = {"from": "2026-11-09", "to": "2026-11-15"}
    await call(
        "search_providers", {"service": "plumber", "area": "indiranagar", "date_range": week}
    )
    p3 = await call("get_provider", {"provider_id": "P3"})
    first = await call("hold_slot", {"provider_id": "P3", "slot": p3["available_slots"][0]})
    second = await call("hold_slot", {"provider_id": "P3", "slot": p3["available_slots"][1]})
    await call("book", {"hold_id": first["hold_id"], "idempotency_key": "k-1"})
    await call("book", {"hold_id": first["hold_id"], "idempotency_key": "k-1"})
    await call("book", {"hold_id": second["hold_id"], "idempotency_key": "k-2"})
    await call("get_booking_status", {"idempotency_key": "k-1"})
    await call("get_booking_status", {"hold_id": second["hold_id"]})
    listing = await call("list_bookings", {"user_id": "u_demo"})
    a2 = next(b for b in listing["bookings"] if b["provider_id"] == "A2")
    await call("cancel", {"booking_id": a2["booking_id"], "idempotency_key": "c-1"})
    await call("cancel", {"booking_id": a2["booking_id"], "idempotency_key": "c-1"})
    await call("search_providers", {"service": "plumber", "area": "nowhere", "date_range": week})
    return out


async def test_in_process_and_http_answer_byte_for_byte(
    tmp_path: Path, http_world: WorldProcess
) -> None:
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(SCENARIO)
    try:
        local = await script(InProcessTransport(host))
    finally:
        host.close()

    async with HttpInspector(http_world.base_url) as inspector:
        await inspector.load_scenario(SCENARIO)
    async with HttpTransport(http_world.base_url) as client:
        remote = await script(client)

    assert len(local) == len(remote)
    for i, (mine, theirs) in enumerate(zip(local, remote, strict=True)):
        assert mine == theirs, f"call {i} differs"
    statuses = [s for s, _ in local]
    assert 503 in statuses and 200 in statuses
