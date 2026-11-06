"""Test-only faults: each one is opt-in and named, and each fails the way it says."""

from __future__ import annotations

import pytest

from errand.tools.errors import ToolTransient, ToolUnexpected
from errand.world.contracts import Area, BookingStatusOut, CancelOut, ListBookingsOut, Service
from errand.world.scenario import (
    BookConfig,
    FaultsConfig,
    MalformedFault,
    ScenarioConfig,
    StatusOutage,
    WorldConfig,
)
from tests.contract.conftest import Env
from tests.contract.helpers import WEEK, book_ok, hold_first_free


def with_faults(faults: FaultsConfig, *forced: str) -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "id": "faults",
            "seed": 3,
            "world": WorldConfig.model_validate(
                {"book": BookConfig.model_validate({"forced": list(forced)})}
            ),
            "faults": faults,
        }
    )


@pytest.mark.parametrize("mode", ["bad_json", "extra_fields", "wrong_types"])
async def test_malformed_get_provider_is_tool_unexpected_with_the_raw_payload(
    env: Env, mode: str
) -> None:
    faults = FaultsConfig(
        malformed=MalformedFault.model_validate({"tool": "get_provider", "mode": mode})
    )
    client = await env.load(with_faults(faults))
    with pytest.raises(ToolUnexpected) as raised:
        await client.get_provider("P3")
    assert raised.value.tool == "get_provider"
    assert raised.value.raw, "the raw payload is attached"
    if mode == "extra_fields":
        assert b"internal_rank_score" in raised.value.raw
    # Other tools are untouched.
    assert isinstance(await client.list_bookings("u_demo"), ListBookingsOut)


async def test_malformed_book_still_applies_at_the_provider(env: Env) -> None:
    faults = FaultsConfig(malformed=MalformedFault(tool="book", mode="wrong_types"))
    client = await env.load(with_faults(faults, "success"))
    held = await hold_first_free(client)
    with pytest.raises(ToolUnexpected):
        await client.book(held.hold_id, "k-mal")
    status = await client.get_booking_status(idempotency_key="k-mal")
    assert isinstance(status, BookingStatusOut) and status.status == "confirmed"


@pytest.mark.parametrize("tool", ["search_providers", "get_provider"])
async def test_read_tool_outage(env: Env, tool: str) -> None:
    client = await env.load(with_faults(FaultsConfig.model_validate({tool: 503})))
    with pytest.raises(ToolTransient) as raised:
        if tool == "search_providers":
            await client.search_providers(Service.PLUMBER, Area.INDIRANAGAR, WEEK)
        else:
            await client.get_provider("P3")
    assert raised.value.status == 503


async def test_status_unavailable_for_the_first_n_calls(env: Env) -> None:
    client = await env.load(
        with_faults(FaultsConfig(get_booking_status=StatusOutage(unavailable_n=2)))
    )
    for _ in range(2):
        with pytest.raises(ToolTransient):
            await client.get_booking_status(idempotency_key="k")
    assert await client.get_booking_status(idempotency_key="k") == BookingStatusOut(
        status="not_found"
    )


async def test_cancel_always_503(env: Env) -> None:
    client = await env.load(with_faults(FaultsConfig(cancel="always_503")))
    listing = await client.list_bookings("u_demo")
    assert isinstance(listing, ListBookingsOut)
    a2 = next(b.booking_id for b in listing.bookings if b.provider_id == "A2")
    for _ in range(3):
        with pytest.raises(ToolTransient):
            await client.cancel(a2, "c-always")


async def test_cancel_succeeds_on_the_retry_without_the_fault(env: Env) -> None:
    client = await env.load()
    listing = await client.list_bookings("u_demo")
    assert isinstance(listing, ListBookingsOut)
    a2 = next(b.booking_id for b in listing.bookings if b.provider_id == "A2")
    with pytest.raises(ToolTransient):
        await client.cancel(a2, "c-once")
    assert isinstance(await client.cancel(a2, "c-once"), CancelOut)


async def test_book_price_mismatch(env: Env) -> None:
    client = await env.load(with_faults(FaultsConfig(book_price_mismatch=100), "success"))
    held = await hold_first_free(client)
    booked = await book_ok(client, held.hold_id, "k-price")
    assert booked.price_inr == held.quoted_price_inr + 100


def test_fault_names_for_the_eval_table() -> None:
    assert ScenarioConfig(id="clean").fault_names() == []
    faults = FaultsConfig(cancel="always_503", duplicate_delivery="book")
    assert ScenarioConfig(id="f", faults=faults).fault_names() == ["cancel", "duplicate_delivery"]
