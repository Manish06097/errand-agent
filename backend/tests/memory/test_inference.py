"""The two inference rules and what the gate does with their suggestions."""

from __future__ import annotations

from datetime import datetime

from errand.clock import IST, to_ms
from errand.memory import inference
from errand.store.repos.known_bookings import KnownBooking
from tests.memory.conftest import CHAT, USER, Env


def _b(
    booking_id: str, provider: str, service: str, day: int, hour: int, status: str = "confirmed"
) -> KnownBooking:
    start = to_ms(datetime(2026, 10, day, hour, 0, tzinfo=IST))
    return KnownBooking(
        booking_id,
        USER,
        provider,
        service,
        start,
        start + 3_600_000,
        status,
        start,
        CHAT,
        None,
        None,
    )


def test_three_of_the_last_four_before_noon_suggests_latest_noon() -> None:
    rows = [
        _b("B0", "P1", "plumber", 1, 15),  # too old to count
        _b("B1", "P1", "plumber", 2, 9),
        _b("B2", "P2", "plumber", 3, 14),
        _b("B3", "P3", "plumber", 4, 10),
        _b("B4", "P9", "plumber", 5, 11),
    ]
    got = inference.latest_time_before_noon(rows, IST)
    assert got is not None
    assert (got.key, got.scope, got.value) == ("latest_time", "global", "12:00")
    assert got.booking_ids == ("B1", "B3", "B4")


def test_two_of_four_or_fewer_than_four_bookings_suggests_nothing() -> None:
    rows = [_b("B1", "P1", "plumber", 2, 9), _b("B2", "P2", "plumber", 3, 14)]
    assert inference.latest_time_before_noon(rows, IST) is None
    rows += [_b("B3", "P3", "plumber", 4, 10), _b("B4", "P9", "plumber", 5, 16)]
    assert inference.latest_time_before_noon(rows, IST) is None


def test_cancelled_bookings_do_not_count() -> None:
    rows = [_b(f"B{i}", "P1", "plumber", i + 1, 9, "cancelled") for i in range(4)]
    assert inference.latest_time_before_noon(rows, IST) is None
    assert inference.same_provider_twice(rows) == []


def test_the_same_provider_twice_for_a_service_suggests_it_for_that_service_only() -> None:
    rows = [
        _b("B1", "E4", "electrician", 2, 9),
        _b("B2", "E5", "electrician", 3, 9),
        _b("B3", "E4", "electrician", 4, 9),
        _b("B4", "P1", "plumber", 5, 9),
    ]
    got = inference.same_provider_twice(rows)
    assert [(c.key, c.scope, c.value, c.booking_ids) for c in got] == [
        ("preferred_provider", "service:electrician", "E4", ("B1", "B3"))
    ]


def test_the_most_booked_provider_wins() -> None:
    rows = [_b(f"B{i}", "E4" if i < 3 else "P6", "electrician", i + 1, 9) for i in range(1, 7)]
    got = inference.same_provider_twice(rows)
    assert got[0].value == "E4" or got[0].value == "P6"
    tally = {"E4": 2, "P6": 4}
    assert tally[got[0].value] == max(tally.values())


async def test_run_inference_writes_pending_rows_with_booking_evidence(env: Env) -> None:
    async with env.db.transaction() as repos:
        for row in [
            _b("B1", "E4", "electrician", 2, 9),
            _b("B2", "E4", "electrician", 3, 10),
            _b("B3", "E5", "plumber", 4, 9),
            _b("B4", "P1", "plumber", 5, 11),
        ]:
            await repos.known_bookings.upsert(row)
    written = await env.service.run_inference(user_id=USER, tz=IST)
    assert [(m["key"], m["scope"], m["status"]) for m in written] == [
        ("latest_time", "global", "pending_confirmation"),
        ("preferred_provider", "service:electrician", "pending_confirmation"),
    ]
    assert written[0]["evidence"] == {
        "type": "rule", "rule": "latest_time_before_noon", "booking_ids": ["B1", "B2", "B3", "B4"],
    }  # fmt: skip
    assert written[0]["source"] == "Inferred from your bookings"
    # a second run adds nothing: both are already pending
    assert await env.service.run_inference(user_id=USER, tz=IST) == []


async def test_an_active_avoid_stops_the_suggestion_for_that_provider(env: Env) -> None:
    await env.remember("avoid_provider", "service:plumber", ["P7"], "I never want P7")
    async with env.db.transaction() as repos:
        await repos.known_bookings.upsert(_b("B1", "P7", "plumber", 2, 14))
        await repos.known_bookings.upsert(_b("B2", "P7", "plumber", 3, 14))
    assert await env.service.run_inference(user_id=USER, tz=IST) == []
