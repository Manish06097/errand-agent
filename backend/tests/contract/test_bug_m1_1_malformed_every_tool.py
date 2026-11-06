"""BUG-M1-1: `malformed` must break every tool's answer, in every mode.

`wrong_types` used to change only top-level scalars, so for search_providers and
list_bookings (whose bodies are a single list) it changed nothing and the client
decoded a valid answer. A scenario using that fault would have passed while proving
nothing. Every tool x mode here has to reach the client as ToolUnexpected with the
raw bytes attached.
"""

from __future__ import annotations

import json

import pytest

from errand.tools.errors import ToolTransient, ToolUnexpected
from errand.world.contracts import TOOLS, Area, ListBookingsOut, Service, ToolName
from errand.world.scenario import (
    BookConfig,
    FaultsConfig,
    MalformedFault,
    ScenarioConfig,
    WorldConfig,
)
from tests.contract.conftest import Client, Env
from tests.contract.helpers import WEEK, first_free_slot, hold_first_free

MODES = ["bad_json", "extra_fields", "wrong_types"]


async def seeded_a2(client: Client) -> str:
    listing = await client.list_bookings("u_demo")
    assert isinstance(listing, ListBookingsOut)
    return next(b.booking_id for b in listing.bookings if b.provider_id == "A2")


async def call(client: Client, tool: ToolName) -> object:
    """Make one well-formed call to `tool` that the world answers with a 200 body."""
    if tool == "search_providers":
        return await client.search_providers(Service.PLUMBER, Area.INDIRANAGAR, WEEK)
    if tool == "get_provider":
        return await client.get_provider("P3")
    if tool == "hold_slot":
        return await client.hold_slot("P3", await first_free_slot(client, "P3"))
    if tool == "book":
        held = await hold_first_free(client)
        return await client.book(held.hold_id, "k-mal")
    if tool == "get_booking_status":
        return await client.get_booking_status(idempotency_key="k-none")
    if tool == "cancel":
        booking_id = await seeded_a2(client)
        # The first cancel per booking is the contract's 503; the retry gets a body.
        with pytest.raises(ToolTransient):
            await client.cancel(booking_id, "c-mal")
        return await client.cancel(booking_id, "c-mal")
    if tool == "reschedule":
        booking_id = await seeded_a2(client)
        target = await first_free_slot(client, "A2", index=3)
        return await client.reschedule(booking_id, target, "r-mal")
    if tool == "list_bookings":
        return await client.list_bookings("u_demo")
    raise AssertionError(tool)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", TOOLS)
async def test_malformed_reaches_the_client_as_tool_unexpected(
    env: Env, tool: ToolName, mode: str
) -> None:
    scenario = ScenarioConfig(
        id="malformed-every-tool",
        seed=3,
        world=WorldConfig(book=BookConfig(forced=["success"])),
        faults=FaultsConfig(malformed=MalformedFault.model_validate({"tool": tool, "mode": mode})),
    )
    client = await env.load(scenario)
    with pytest.raises(ToolUnexpected) as raised:
        answer = await call(client, tool)
        pytest.fail(f"{tool} with malformed={mode} decoded as a valid {type(answer).__name__}")
    assert raised.value.tool == tool
    assert raised.value.raw, "the raw payload is attached"
    if mode == "bad_json":
        with pytest.raises(ValueError):
            json.loads(raised.value.raw)
    else:
        decoded = json.loads(raised.value.raw)
        assert isinstance(decoded, dict)
        if mode == "extra_fields":
            assert "internal_rank_score" in decoded


@pytest.mark.parametrize("mode", MODES)
async def test_malformed_breaks_a_body_with_no_scalars(env: Env, mode: str) -> None:
    # {"bookings": []} has no scalar at any depth to retype.
    scenario = ScenarioConfig(
        id="malformed-empty-body",
        seed=3,
        faults=FaultsConfig(
            malformed=MalformedFault.model_validate({"tool": "list_bookings", "mode": mode})
        ),
    )
    client = await env.load(scenario)
    with pytest.raises(ToolUnexpected) as raised:
        answer = await client.list_bookings("u_nobody")
        pytest.fail(f"malformed={mode} decoded as a valid {type(answer).__name__}")
    assert raised.value.raw
