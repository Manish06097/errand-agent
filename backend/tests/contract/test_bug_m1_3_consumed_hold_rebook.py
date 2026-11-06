"""BUG-M1-3: a consumed hold must never produce a second booking under another key.

`_book` only asked whether another key's live booking sat on the hold's slot. Once
the consuming booking was cancelled (or moved away by reschedule), the slot was free
again and `book(H, k2)` created a second booking on the already-used hold H. The
world then contradicted itself: `get_booking_status(hold_id=H)` reported the
earliest, cancelled booking while H also had a live confirmed one.

The rule: a different key on a consumed hold gets `slot_taken` while the consuming
booking is live, and `invalid_args` "hold already used" otherwise.
"""

from __future__ import annotations

import pytest

from errand.tools.errors import ToolTransient
from errand.world.contracts import BookError, BookingStatusOut, CancelOut, RescheduleOut
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from tests.contract.conftest import Env
from tests.contract.helpers import book_ok, first_free_slot, hold_first_free, rows

HOLD_USED = BookError(error="invalid_args", reason="hold already used")
SLOT_TAKEN = BookError(error="slot_taken")


@pytest.mark.parametrize("dedupe", [True, False], ids=["dedupe_on", "dedupe_off"])
@pytest.mark.parametrize("after", ["cancelled", "cancelled_by_provider", "rescheduled"])
async def test_a_consumed_hold_never_books_under_another_key(
    env: Env, after: str, dedupe: bool
) -> None:
    # Every draw would succeed, so any second booking would really be created.
    world = WorldConfig(
        enforce_idempotency=dedupe,
        book=BookConfig.model_validate({"forced": ["success", "success"]}),
    )
    client = await env.load(ScenarioConfig(id="consumed-hold", seed=7, world=world))
    held = await hold_first_free(client)
    first = await book_ok(client, held.hold_id, "k1")

    if after == "cancelled":
        with pytest.raises(ToolTransient):
            await client.cancel(first.booking_id, "c-1")
        assert await client.cancel(first.booking_id, "c-1") == CancelOut(
            status="cancelled", booking_id=first.booking_id
        )
        expected, final_status = HOLD_USED, "cancelled"
    elif after == "cancelled_by_provider":
        await env.inspector.fire_provider_cancel(first.booking_id)
        expected, final_status = HOLD_USED, "cancelled_by_provider"
    else:
        target = await first_free_slot(client, "P3")
        assert isinstance(await client.reschedule(first.booking_id, target, "r-1"), RescheduleOut)
        # The consuming booking is still live, just elsewhere.
        expected, final_status = SLOT_TAKEN, "confirmed"

    assert await client.book(held.hold_id, "k2") == expected

    on_hold = [b for b in await env.inspector.bookings() if b.hold_id == held.hold_id]
    assert [b.booking_id for b in on_hold] == [first.booking_id]
    assert [r.outcome for r in rows(await env.inspector.op_log(), "book", "k2")] == [expected.error]
    [op] = [o for o in await env.inspector.operations() if o.idempotency_key == "k2"]
    assert op.outcome == "not_applied" and op.booking_id is None
    # The world's two answers about H agree: one booking, and the lookup names it.
    status = await client.get_booking_status(hold_id=held.hold_id)
    assert isinstance(status, BookingStatusOut)
    assert (status.booking_id, status.status) == (first.booking_id, final_status)
    assert await client.get_booking_status(idempotency_key="k2") == BookingStatusOut(
        status="not_found"
    )


async def test_a_different_key_while_the_consuming_booking_holds_the_slot(env: Env) -> None:
    world = WorldConfig(book=BookConfig.model_validate({"forced": ["success", "success"]}))
    client = await env.load(ScenarioConfig(id="consumed-live", seed=7, world=world))
    held = await hold_first_free(client)
    first = await book_ok(client, held.hold_id, "k1")
    assert await client.book(held.hold_id, "k2") == SLOT_TAKEN
    on_hold = [b for b in await env.inspector.bookings() if b.hold_id == held.hold_id]
    assert [b.booking_id for b in on_hold] == [first.booking_id]
