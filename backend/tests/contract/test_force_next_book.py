"""The dev drawer's "next book times out after success" (M7): `force_next_book` pushes one
outcome onto the next `book` without loading a scenario, so the booking in progress, the
holds and the clock all survive. Same behaviour on both transports."""

from __future__ import annotations

from errand.tools.errors import ToolTimeout
from errand.world.contracts import BookError
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from tests.contract.conftest import Env
from tests.contract.helpers import book_ok, hold_first_free


async def test_a_pushed_outcome_applies_to_the_next_book_only(env: Env) -> None:
    world = WorldConfig(book=BookConfig.model_validate({"forced": ["success", "success"]}))
    client = await env.load(ScenarioConfig(id="force-next", seed=7, world=world))
    held = await hold_first_free(client)
    before = await env.inspector.now()

    await env.inspector.force_next_book("timeout_booked")

    # Nothing was reset: the hold still exists and the clock did not move back.
    assert await env.inspector.now() >= before
    try:
        await client.book(held.hold_id, "k-forced")
    except ToolTimeout:
        pass
    else:  # pragma: no cover - the forced outcome must time out
        raise AssertionError("the pushed timeout_booked outcome did not reach book")
    bookings = await env.inspector.bookings()
    assert [b.status for b in bookings if b.hold_id == held.hold_id] == ["confirmed"]

    # The scenario's own forced list resumes afterwards, untouched by the push.
    second = await hold_first_free(client)
    assert (await book_ok(client, second.hold_id, "k-next")).booking_id


async def test_a_pushed_slot_taken_is_a_clean_refusal(env: Env) -> None:
    world = WorldConfig(book=BookConfig.model_validate({"forced": ["success"]}))
    client = await env.load(ScenarioConfig(id="force-taken", seed=7, world=world))
    held = await hold_first_free(client)
    await env.inspector.force_next_book("slot_taken")
    assert await client.book(held.hold_id, "k-taken") == BookError(error="slot_taken")
    assert [b for b in await env.inspector.bookings() if b.hold_id == held.hold_id] == []
