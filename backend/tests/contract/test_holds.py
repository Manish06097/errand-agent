"""Hold TTL, `hold_expired` only past expiry, no supersede, price drift, slot shift."""

from __future__ import annotations

from datetime import timedelta

from errand.world.contracts import BookError, BookOut, HoldSlotOut
from errand.world.scenario import (
    BookConfig,
    FaultsConfig,
    QuoteDrift,
    ScenarioConfig,
    WorldConfig,
)
from tests.contract.conftest import Env
from tests.contract.helpers import book_ok, first_free_slot, hold_first_free, provider


def forced_success(n: int = 4, **world: object) -> ScenarioConfig:
    return ScenarioConfig(
        id="holds",
        seed=5,
        world=WorldConfig.model_validate({"book": BookConfig(forced=["success"] * n), **world}),
    )


async def test_hold_lasts_exactly_the_ttl(env: Env) -> None:
    client = await env.load()
    before = await env.inspector.now()
    held = await hold_first_free(client)
    assert held.expires_at - before == timedelta(minutes=10)
    assert held.expires_at.utcoffset() == timedelta(hours=5, minutes=30)


async def test_hold_ttl_is_configurable(env: Env) -> None:
    client = await env.load(ScenarioConfig(id="ttl", seed=5, world=WorldConfig(hold_ttl_s=90)))
    before = await env.inspector.now()
    held = await hold_first_free(client)
    assert held.expires_at - before == timedelta(seconds=90)


async def test_book_just_before_expiry_still_books(env: Env) -> None:
    client = await env.load(forced_success())
    held = await hold_first_free(client)
    now = await env.inspector.now()
    # One millisecond of hold left (in-process, exact); over HTTP the same instant
    # is reached through /dev/clock/advance on a simulated clock, so it is exact too.
    await env.inspector.advance((held.expires_at - now).total_seconds() - 0.001)
    assert isinstance(await client.book(held.hold_id, "k-edge"), BookOut)


async def test_book_at_expiry_is_hold_expired(env: Env) -> None:
    client = await env.load(forced_success())
    held = await hold_first_free(client)
    now = await env.inspector.now()
    await env.inspector.advance((held.expires_at - now).total_seconds())
    assert await client.book(held.hold_id, "k-at") == BookError(error="hold_expired")
    # The forced success was not consumed by the expired call.
    again = await hold_first_free(client)
    assert isinstance(await client.book(again.hold_id, "k-next"), BookOut)


async def test_hold_expired_after_eleven_minutes(env: Env) -> None:
    client = await env.load(forced_success())
    held = await hold_first_free(client)
    await env.inspector.advance(11 * 60)
    assert await client.book(held.hold_id, "k-late") == BookError(error="hold_expired")


async def test_a_second_hold_does_not_supersede_the_first(env: Env) -> None:
    client = await env.load(forced_success())
    slot = await first_free_slot(client, "P3")
    first = await client.hold_slot("P3", slot)
    second = await client.hold_slot("P3", slot)
    assert isinstance(first, HoldSlotOut) and isinstance(second, HoldSlotOut)
    assert first.hold_id != second.hold_id
    booked = await book_ok(client, first.hold_id, "k-first")
    assert booked.slot == slot
    # The second hold was never superseded; its slot is now booked, so slot_taken.
    assert await client.book(second.hold_id, "k-second") == BookError(error="slot_taken")
    # And the booked slot is no longer offered.
    assert slot not in (await provider(client, "P3")).available_slots


async def test_released_hold_answers_slot_taken_not_hold_expired(env: Env) -> None:
    scenario = ScenarioConfig(
        id="released", seed=5, world=WorldConfig(book=BookConfig(forced=["slot_taken"]))
    )
    client = await env.load(scenario)
    held = await hold_first_free(client)
    assert await client.book(held.hold_id, "k-taken") == BookError(error="slot_taken")
    assert await client.book(held.hold_id, "k-other") == BookError(error="slot_taken")
    await env.inspector.advance(11 * 60)
    # Past expiry the expiry predicate wins, as the contract says.
    assert await client.book(held.hold_id, "k-later") == BookError(error="hold_expired")


async def test_price_drift_on_hold(env: Env) -> None:
    drift = QuoteDrift(mode="on_hold", providers={"P3": 250})
    client = await env.load(
        ScenarioConfig(id="drift", seed=5, world=WorldConfig(quote_drift=drift))
    )
    base = (await provider(client, "P3")).price_inr
    held = await hold_first_free(client, "P3")
    assert held.quoted_price_inr == base + 250
    other = await hold_first_free(client, "P9")
    assert other.quoted_price_inr == (await provider(client, "P9")).price_inr


async def test_price_drift_on_rehold(env: Env) -> None:
    drift = QuoteDrift(mode="on_rehold", providers={"P3": 250})
    client = await env.load(
        ScenarioConfig(id="redrift", seed=5, world=WorldConfig(quote_drift=drift))
    )
    base = (await provider(client, "P3")).price_inr
    slot = await first_free_slot(client, "P3")
    first = await client.hold_slot("P3", slot)
    second = await client.hold_slot("P3", slot)
    assert isinstance(first, HoldSlotOut) and isinstance(second, HoldSlotOut)
    assert first.quoted_price_inr == base
    assert second.quoted_price_inr == base + 250
    # A different slot's first hold is undrifted.
    assert (await hold_first_free(client, "P3", index=1)).quoted_price_inr == base


async def test_random_pct_drift_is_keyed_and_bounded(env: Env) -> None:
    drift = QuoteDrift(mode="random_pct", pct=10)
    scenario = ScenarioConfig(id="pct", seed=5, world=WorldConfig(quote_drift=drift))
    client = await env.load(scenario)
    base = (await provider(client, "P3")).price_inr
    quotes = [(await hold_first_free(client, "P3", index=i)).quoted_price_inr for i in range(5)]
    assert all(abs(q - base) <= base * 0.1 + 1 for q in quotes)
    client = await env.load(scenario)
    assert [
        (await hold_first_free(client, "P3", index=i)).quoted_price_inr for i in range(5)
    ] == quotes


async def test_rehold_slot_shift_fault(env: Env) -> None:
    faults = FaultsConfig(rehold_slot_shift={"P3": "+30m"})
    client = await env.load(ScenarioConfig(id="shift", seed=5, faults=faults))
    slot = await first_free_slot(client, "P3")
    first = await client.hold_slot("P3", slot)
    second = await client.hold_slot("P3", slot)
    assert isinstance(first, HoldSlotOut) and isinstance(second, HoldSlotOut)
    assert first.slot == slot
    assert second.slot.start == slot.start + timedelta(minutes=30)
    assert second.slot.end == slot.end + timedelta(minutes=30)
