"""BUG-M1-2: with dedupe off, a re-sent book must not rewrite an applied key's record.

`finish_operation` wrote whatever the latest evaluation said, so a re-sent `book` on
an applied key whose second evaluation did not book (slot_taken, timeout_not_booked,
hold_expired) flipped `operations.outcome` from applied to not_applied while the
key's booking still existed. A second success overwrote `response_json` with the
second booking while `booking_id` kept the first. Either way the world's own record
of the key disagreed with `bookings`, the ground truth INV-02 and INV-03 read.
"""

from __future__ import annotations

import pytest

from errand.tools.errors import ToolTimeout
from errand.tools.timeouts import ToolTimeouts
from errand.world.contracts import BookError, BookOut
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from tests.contract.conftest import Env
from tests.contract.helpers import hold_first_free, raw, rows

KEY = "k-off-applied"


@pytest.mark.parametrize("second", ["success", "slot_taken", "timeout_not_booked", "hold_expired"])
async def test_a_resent_book_never_rewrites_an_applied_key(env: Env, second: str) -> None:
    forced = ["success"] if second == "hold_expired" else ["success", second]
    world = WorldConfig(
        enforce_idempotency=False, book=BookConfig.model_validate({"forced": forced})
    )
    client = await env.load(
        ScenarioConfig(id="dedupe-off-applied", seed=5, world=world),
        timeouts=ToolTimeouts(book=0.5),
    )
    held = await hold_first_free(client)
    status, first = await raw(client, "book", {"hold_id": held.hold_id, "idempotency_key": KEY})
    assert status == 200
    first_booking = BookOut.model_validate_json(first).booking_id

    if second == "hold_expired":
        await env.inspector.advance(601)
    if second == "timeout_not_booked":
        with pytest.raises(ToolTimeout):
            await client.book(held.hold_id, KEY)
    else:
        again = await client.book(held.hold_id, KEY)
        if second == "success":
            assert isinstance(again, BookOut) and again.booking_id != first_booking
        else:
            assert again == BookError.model_validate({"error": second})

    # The re-send really was evaluated again; that part of dedupe-off is intended.
    log = rows(await env.inspector.op_log(), "book", KEY)
    assert [r.outcome for r in log] == ["success", second]
    # The key's record still describes the application that happened first.
    [op] = [o for o in await env.inspector.operations() if o.idempotency_key == KEY]
    assert op.outcome == "applied"
    assert op.booking_id == first_booking
    assert op.response_json == first.decode()
    assert (op.calls, op.attempts) == (2, 2)
    assert first_booking in {b.booking_id for b in await env.inspector.bookings()}
