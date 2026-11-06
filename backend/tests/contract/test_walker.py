"""With an empty `faults` block, nothing behaves outside the brief's table.

The walker drives every tool across many seeds of the default world and checks each
raw response against the brief's allowed answers for that tool and the exact key
sets of architecture 5.2. The key sets are written out here on purpose, not taken
from the wire models, so a field added to a model (and so to both sides) still fails.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from errand.tools.errors import ToolTimeout
from errand.tools.timeouts import ToolTimeouts
from errand.world.catalog import BANGALORE_V1
from errand.world.contracts import ToolName
from errand.world.scenario import ScenarioConfig
from tests.contract.conftest import Client, Env

SLOT = {"start", "end"}
ATTRIBUTES = {"verified", "years_experience", "languages", "brands"}
SUMMARY = {
    "provider_id", "name", "service", "area", "price_inr", "rating", "review_count",
    "available_slots", "attributes",
}  # fmt: skip
DETAILS = {
    "provider_id", "name", "service", "area", "price_inr", "rating", "attributes", "about",
    "available_slots", "reviews",
}  # fmt: skip
REVIEW = {"review_id", "rating", "text", "posted_at"}
HOLD = {"hold_id", "provider_id", "slot", "quoted_price_inr", "expires_at"}
BOOKED = {"status", "booking_id", "provider_id", "slot", "price_inr"}
BOOKING_ITEM = {"booking_id", "provider_id", "provider_name", "service", "slot", "status"}
UNAVAILABLE = {"error": "service_unavailable", "http_status": 503, "retryable": True}

# Every answer the brief allows each tool to give in the default world.
ALLOWED: dict[ToolName, set[str]] = {
    "search_providers": {"ok"},
    "get_provider": {"ok"},
    "hold_slot": {"ok"},
    "book": {"confirmed", "slot_taken", "hold_expired", "timeout"},
    "get_booking_status": {"confirmed", "not_found", "cancelled", "cancelled_by_provider"},
    "cancel": {"cancelled", "transient_503"},
    "reschedule": {"rescheduled", "slot_conflict"},
    "list_bookings": {"ok"},
}


def slot_ok(slot: Any) -> None:
    assert set(slot) == SLOT
    assert slot["start"].endswith("+05:30") and slot["end"].endswith("+05:30")


def classify(tool: ToolName, status: int, raw: bytes) -> str:
    """Name the answer, asserting its exact shape; anything off-table fails here."""
    if status == 503:
        assert tool == "cancel", f"{tool} answered 503 with no fault configured"
        assert json.loads(raw) == UNAVAILABLE
        return "transient_503"
    assert status == 200, (tool, status, raw)
    assert b"is_injection" not in raw
    data = json.loads(raw)
    if "error" in data:
        assert set(data) == {"error"}, (tool, data)
        return str(data["error"])
    if tool == "search_providers":
        assert set(data) == {"providers"} and 3 <= len(data["providers"]) <= 8
        for p in data["providers"]:
            assert set(p) == SUMMARY and set(p["attributes"]) == ATTRIBUTES
            assert isinstance(p["price_inr"], int) and not isinstance(p["price_inr"], bool)
            for s in p["available_slots"]:
                slot_ok(s)
        return "ok"
    if tool == "get_provider":
        assert set(data) == DETAILS and set(data["attributes"]) == ATTRIBUTES
        assert all(set(r) == REVIEW for r in data["reviews"])
        return "ok"
    if tool == "hold_slot":
        assert set(data) == HOLD
        slot_ok(data["slot"])
        assert isinstance(data["quoted_price_inr"], int)
        return "ok"
    if tool == "book":
        assert set(data) == BOOKED and data["status"] == "confirmed"
        return "confirmed"
    if tool == "get_booking_status":
        keys = {"status", "booking_id", "slot"} if data["status"] != "not_found" else {"status"}
        assert set(data) == keys, data
        return str(data["status"])
    if tool == "cancel":
        assert set(data) == {"status", "booking_id"}
        return str(data["status"])
    if tool == "reschedule":
        assert set(data) == {"status", "booking_id", "slot"}
        return str(data["status"])
    assert tool == "list_bookings"
    assert set(data) == {"bookings"}
    for b in data["bookings"]:
        assert set(b) == BOOKING_ITEM
        assert b["status"] in {"confirmed", "completed", "cancelled", "cancelled_by_provider"}
    return "ok"


class Walker:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.seen: Counter[tuple[str, str]] = Counter()

    async def call(self, tool: ToolName, args: dict[str, Any]) -> tuple[str, Any]:
        try:
            status, raw = await self.client._send(tool, json.dumps(args).encode())
        except ToolTimeout:
            label, data = "timeout", None
        else:
            label = classify(tool, status, raw)
            data = json.loads(raw)
        assert label in ALLOWED[tool], (tool, label)
        self.seen[(tool, label)] += 1
        return label, data


async def walk(env: Env, seed: int, walker_counts: Counter[tuple[str, str]]) -> None:
    scenario = ScenarioConfig(id=f"walk-{seed}", seed=seed)
    client = await env.load(scenario, timeouts=ToolTimeouts(book=0.3))
    w = Walker(client)
    week = {"from": "2026-11-09", "to": "2026-11-15"}
    listed: list[str] = []
    for service, area in BANGALORE_V1:
        args = {"service": service, "area": area, "date_range": week}
        _, data = await w.call("search_providers", args)
        listed += [p["provider_id"] for p in data["providers"]]
    details: dict[str, Any] = {}
    for pid in {*listed[:: max(1, len(listed) // 12)], "P3", "P9", "E4", "A2", "P1"}:
        _, details[pid] = await w.call("get_provider", {"provider_id": pid})

    holds = []
    for i, pid in enumerate(["P3", "P9", "E4", "A2"]):
        args = {"provider_id": pid, "slot": details[pid]["available_slots"][i]}
        _, hold = await w.call("hold_slot", args)
        key = f"walk-{seed}-{i}"
        holds.append((hold["hold_id"], key))
        await w.call("book", {"hold_id": hold["hold_id"], "idempotency_key": key})
    for hold_id, key in holds:
        await w.call("get_booking_status", {"hold_id": hold_id})
        await w.call("get_booking_status", {"idempotency_key": key})

    _, listing = await w.call("list_bookings", {"user_id": "u_demo"})
    a2 = next(
        b
        for b in listing["bookings"]
        if b["provider_id"] == "A2" and b["slot"]["start"].startswith("2026-11-12")
    )
    for target in details["A2"]["available_slots"][5:7]:
        args = {"booking_id": a2["booking_id"], "slot": target, "idempotency_key": target["start"]}
        await w.call("reschedule", args)
    for booking in [b for b in listing["bookings"] if b["status"] == "confirmed"]:
        args = {
            "booking_id": booking["booking_id"],
            "idempotency_key": f"c-{booking['booking_id']}",
        }
        for _ in range(2):
            await w.call("cancel", args)
    await w.call("list_bookings", {"user_id": "u_demo"})

    slot = details["P1"]["available_slots"][-1]
    _, late = await w.call("hold_slot", {"provider_id": "P1", "slot": slot})
    await env.inspector.advance(11 * 60)
    label, _ = await w.call("book", {"hold_id": late["hold_id"], "idempotency_key": f"late-{seed}"})
    assert label == "hold_expired"
    walker_counts.update(w.seen)


async def test_empty_faults_world_stays_on_the_brief_table(env: Env) -> None:
    seeds = range(30) if env.kind == "inprocess" else range(4)
    seen: Counter[tuple[str, str]] = Counter()
    for seed in seeds:
        await walk(env, seed, seen)
    # The walk really exercised the contract's answers, not just the happy path.
    expected = {
        ("cancel", "transient_503"),
        ("cancel", "cancelled"),
        ("book", "confirmed"),
        ("book", "hold_expired"),
        ("get_booking_status", "not_found"),
        ("get_booking_status", "confirmed"),
        ("reschedule", "rescheduled"),
    }
    if env.kind == "inprocess":
        expected |= {("book", "slot_taken"), ("book", "timeout")}
    assert expected <= set(seen), sorted(set(seen))
