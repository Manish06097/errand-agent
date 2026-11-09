"""Cancel, reschedule, list_bookings, get_booking_status and provider-cancel events."""

from __future__ import annotations

from datetime import timedelta

import pytest

from errand.tools.errors import ToolTransient
from errand.world.contracts import (
    BookingStatusError,
    BookingStatusOut,
    CancelError,
    CancelOut,
    ListBookingsOut,
    RescheduleError,
    RescheduleOut,
)
from errand.world.scenario import BookConfig, EventConfig, ScenarioConfig, WorldConfig
from tests.contract.conftest import Client, Env
from tests.contract.helpers import (
    body,
    book_ok,
    first_free_slot,
    hold_first_free,
    provider,
    raw,
    rows,
)

USER = "u_demo"


async def seeded(client: Client, provider_id: str) -> str:
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    return next(b.booking_id for b in listing.bookings if b.provider_id == provider_id)


async def test_list_bookings_shows_the_seeded_history(env: Env) -> None:
    client = await env.load()
    status, data = await raw(client, "list_bookings", {"user_id": USER})
    assert status == 200
    items = body(data)["bookings"]
    for item in items:
        assert set(item) == {
            "booking_id",
            "provider_id",
            "provider_name",
            "service",
            "slot",
            "status",
        }
    summary = {(b["provider_id"], b["service"], b["status"]) for b in items}
    assert summary == {("E4", "electrician", "completed"), ("A2", "ac_servicing", "confirmed")}
    a2 = next(b for b in items if b["provider_id"] == "A2")
    assert a2["slot"] == {"start": "2026-11-12T11:00:00+05:30", "end": "2026-11-12T12:00:00+05:30"}
    assert await client.list_bookings("u_nobody") == ListBookingsOut(bookings=[])


async def test_cancel_fails_once_with_a_real_503_then_succeeds(env: Env) -> None:
    client = await env.load()
    booking_id = await seeded(client, "A2")
    with pytest.raises(ToolTransient) as first:
        await client.cancel(booking_id, "c-1")
    assert first.value.status == 503
    assert body(first.value.body) == {
        "error": "service_unavailable",
        "http_status": 503,
        "retryable": True,
    }
    assert await client.cancel(booking_id, "c-1") == CancelOut(
        status="cancelled", booking_id=booking_id
    )
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    assert {b.status for b in listing.bookings if b.booking_id == booking_id} == {"cancelled"}
    log = rows(await env.inspector.op_log(), "cancel")
    assert [(r.outcome, r.http_status) for r in log] == [("service_unavailable", 503), ("ok", 200)]
    # A replay of the applied cancel key is a no-op.
    assert await client.cancel(booking_id, "c-1") == CancelOut(
        status="cancelled", booking_id=booking_id
    )
    assert rows(await env.inspector.op_log(), "cancel")[-1].outcome == "replayed"


async def test_the_503_is_per_booking(env: Env) -> None:
    client = await env.load(
        ScenarioConfig(id="c2", seed=5, world=WorldConfig(book=BookConfig(forced=["success"])))
    )
    held = await hold_first_free(client)
    booked = await book_ok(client, held.hold_id, "k-c2")
    with pytest.raises(ToolTransient):
        await client.cancel(booked.booking_id, "c-new")
    assert isinstance(await client.cancel(booked.booking_id, "c-new"), CancelOut)
    a2 = await seeded(client, "A2")
    with pytest.raises(ToolTransient):
        await client.cancel(a2, "c-a2")


async def test_cancel_of_an_unknown_booking_is_invalid_args(env: Env) -> None:
    client = await env.load()
    answer = await client.cancel("B-nope", "c-x")
    assert isinstance(answer, CancelError) and answer.error == "invalid_args"


async def test_reschedule_moves_the_booking(env: Env) -> None:
    client = await env.load()
    booking_id = await seeded(client, "A2")
    target = await first_free_slot(client, "A2", index=3)
    answer = await client.reschedule(booking_id, target, "r-1")
    assert answer == RescheduleOut(status="rescheduled", booking_id=booking_id, slot=target)
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    assert next(b.slot for b in listing.bookings if b.booking_id == booking_id) == target
    assert target not in (await provider(client, "A2")).available_slots


async def test_reschedule_conflict_on_a_configured_slot(env: Env) -> None:
    probe = await env.load()
    target = await first_free_slot(probe, "A2", index=2)
    conflict = f"A2@{target.start.isoformat()}"
    client = await env.load(
        ScenarioConfig(id="rc", seed=7, world=WorldConfig(reschedule_conflict_slots=[conflict]))
    )
    booking_id = await seeded(client, "A2")
    assert await client.reschedule(booking_id, target, "r-c") == RescheduleError(
        error="slot_conflict"
    )
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    a2 = next(b for b in listing.bookings if b.booking_id == booking_id)
    assert a2.slot.start.isoformat() == "2026-11-12T11:00:00+05:30", "nothing changed"
    # The configured conflict is not a key-level failure: a different slot still works.
    other = await first_free_slot(client, "A2", index=4)
    assert isinstance(await client.reschedule(booking_id, other, "r-d"), RescheduleOut)


async def test_reschedule_conflict_on_a_genuinely_taken_slot(env: Env) -> None:
    client = await env.load(
        ScenarioConfig(id="rt", seed=7, world=WorldConfig(book=BookConfig(forced=["success"])))
    )
    booking_id = await seeded(client, "A2")
    held = await hold_first_free(client, "A2", index=1)
    await book_ok(client, held.hold_id, "k-taken")
    assert await client.reschedule(booking_id, held.slot, "r-t") == RescheduleError(
        error="slot_conflict"
    )


async def test_status_by_hold_id_and_by_key(env: Env) -> None:
    client = await env.load(
        ScenarioConfig(id="st", seed=7, world=WorldConfig(book=BookConfig(forced=["success"])))
    )
    held = await hold_first_free(client)
    assert await client.get_booking_status(hold_id=held.hold_id) == BookingStatusOut(
        status="not_found"
    )
    booked = await book_ok(client, held.hold_id, "k-st")
    expected = BookingStatusOut(status="confirmed", booking_id=booked.booking_id, slot=booked.slot)
    assert await client.get_booking_status(hold_id=held.hold_id) == expected
    assert await client.get_booking_status(idempotency_key="k-st") == expected
    _, data = await raw(client, "get_booking_status", {"idempotency_key": "k-unknown"})
    assert body(data) == {"status": "not_found"}


@pytest.mark.parametrize(
    "args",
    [{}, {"hold_id": "H-1", "idempotency_key": "k"}, {"hold_id": None, "idempotency_key": None}],
)
async def test_status_needs_exactly_one_lookup(env: Env, args: dict[str, object]) -> None:
    client = await env.load()
    status, data = await raw(client, "get_booking_status", args)
    assert status == 200
    assert BookingStatusError.model_validate_json(data).error == "invalid_args"


async def test_provider_cancel_event_fires_lazily(env: Env) -> None:
    event = EventConfig(type="provider_cancelled", booking="upcoming:0", fire_at="+30m")
    client = await env.load(ScenarioConfig(id="ev", seed=7, world=WorldConfig(events=[event])))
    booking_id = await seeded(client, "A2")
    (scheduled,) = await env.inspector.events()
    assert scheduled.target == {"booking_id": booking_id} and scheduled.fired_at is None
    await env.inspector.advance(29 * 60)
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    assert {b.status for b in listing.bookings if b.booking_id == booking_id} == {"confirmed"}
    await env.inspector.advance(2 * 60)
    # No tick: the next world call fires it.
    listing = await client.list_bookings(USER)
    assert isinstance(listing, ListBookingsOut)
    assert {b.status for b in listing.bookings if b.booking_id == booking_id} == {
        "cancelled_by_provider"
    }
    fired = [r for r in await env.inspector.op_log() if r.op == "event.provider_cancelled"]
    assert [r.outcome for r in fired] == ["fired"]
    (done,) = await env.inspector.events()
    assert done.fired_at is not None and done.fired_at >= done.fire_at


async def test_provider_cancel_on_tick_and_on_demand(env: Env) -> None:
    event = EventConfig(type="provider_cancelled", booking="upcoming:0", fire_at="+1h")
    client = await env.load(ScenarioConfig(id="tick", seed=7, world=WorldConfig(events=[event])))
    booking_id = await seeded(client, "A2")
    assert await env.inspector.tick() == []
    await env.inspector.advance(timedelta(hours=1).total_seconds())
    (fired,) = await env.inspector.tick()
    assert fired.target == {"booking_id": booking_id} and fired.fired_at is not None
    status = await client.get_booking_status(idempotency_key="none")
    assert status == BookingStatusOut(status="not_found")

    # The dev drawer's "provider cancels booking" fires one immediately.
    client = await env.load(
        ScenarioConfig(id="now", seed=7, world=WorldConfig(book=BookConfig(forced=["success"])))
    )
    held = await hold_first_free(client)
    booked = await book_ok(client, held.hold_id, "k-ev")
    event_row = await env.inspector.fire_provider_cancel(booked.booking_id)
    assert event_row.fired_at is not None
    status = await client.get_booking_status(idempotency_key="k-ev")
    assert isinstance(status, BookingStatusOut) and status.status == "cancelled_by_provider"


async def test_event_on_a_created_booking_fires_after_its_creation(env: Env) -> None:
    """`created:N` (M5): the Nth booking the world creates is cancelled by the provider a
    duration after it was created. Seeded history does not count towards N, and a second
    booking is left alone."""
    event = EventConfig(type="provider_cancelled", booking="created:0", fire_at="+5s")
    scenario = ScenarioConfig(
        id="created",
        seed=7,
        world=WorldConfig(book=BookConfig(forced=["success", "success"]), events=[event]),
    )
    client = await env.load(scenario)
    (waiting,) = await env.inspector.events()
    assert waiting.fired_at is None and waiting.target.get("created") == 0
    await env.inspector.advance(3600)  # an unarmed event never fires, whatever the time
    assert await env.inspector.tick() == []

    first = await hold_first_free(client, "P3")
    one = await book_ok(client, first.hold_id, "k-created-1")
    other = await hold_first_free(client, "P5")
    two = await book_ok(client, other.hold_id, "k-created-2")
    (armed,) = await env.inspector.events()
    assert armed.target == {"booking_id": one.booking_id}
    await env.inspector.advance(4)
    status = await client.get_booking_status(idempotency_key="k-created-1")
    assert isinstance(status, BookingStatusOut) and status.status == "confirmed"
    await env.inspector.advance(2)
    status = await client.get_booking_status(idempotency_key="k-created-1")
    assert isinstance(status, BookingStatusOut) and status.status == "cancelled_by_provider"
    untouched = await client.get_booking_status(idempotency_key="k-created-2")
    assert isinstance(untouched, BookingStatusOut) and untouched.booking_id == two.booking_id
    assert untouched.status == "confirmed"
    (done,) = await env.inspector.events()
    assert done.fired_at is not None and done.fired_at >= done.fire_at
