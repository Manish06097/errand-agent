"""Small helpers shared by the contract tests."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

from errand.tools.inspector import HttpInspector, InProcessInspector
from errand.world.contracts import (
    BookOut,
    DateRange,
    GetProviderOut,
    HoldSlotOut,
    Slot,
    ToolName,
)
from errand.world.store import OpLogRow
from tests.contract.conftest import Client

# The default scenario's clock starts on Mon 9 Nov 2026.
WEEK = DateRange.model_validate({"from": date(2026, 11, 9), "to": date(2026, 11, 15)})


def week_of(start: date) -> DateRange:
    return DateRange.model_validate({"from": start, "to": start + timedelta(days=6)}, strict=False)


async def raw(client: Client, tool: ToolName, args: dict[str, Any]) -> tuple[int, bytes]:
    """Send hand-written JSON through the transport and return the status and raw bytes."""
    return await client._send(tool, json.dumps(args).encode())


def body(data: bytes) -> Any:
    return json.loads(data)


async def provider(client: Client, provider_id: str) -> GetProviderOut:
    result = await client.get_provider(provider_id)
    assert isinstance(result, GetProviderOut), result
    return result


async def hold_first_free(client: Client, provider_id: str = "P3", index: int = 0) -> HoldSlotOut:
    details = await provider(client, provider_id)
    held = await client.hold_slot(provider_id, details.available_slots[index])
    assert isinstance(held, HoldSlotOut), held
    return held


async def first_free_slot(client: Client, provider_id: str, index: int = 0) -> Slot:
    return (await provider(client, provider_id)).available_slots[index]


async def book_ok(client: Client, hold_id: str, key: str) -> BookOut:
    result = await client.book(hold_id, key)
    assert isinstance(result, BookOut), result
    return result


def rows(log: list[OpLogRow], op: str, key: str | None = None) -> list[OpLogRow]:
    return [r for r in log if r.op == op and (key is None or r.idempotency_key == key)]


async def wait_for_outcome(
    inspector: InProcessInspector | HttpInspector, op: str, key: str, outcome: str
) -> OpLogRow:
    """Poll the op_log until the call on `key` shows `outcome` (HTTP only needs this)."""
    for _ in range(200):
        for row in rows(await inspector.op_log(), op, key):
            if row.outcome == outcome:
                return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"{op} on {key} never reached {outcome}")
