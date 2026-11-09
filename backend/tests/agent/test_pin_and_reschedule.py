"""History and reschedule, the pure parts (N1): pinning a provider from a booking, the
rule that a pinned provider needs no budget, and which slot a reschedule card proposes."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from errand.agent import brief_ops
from errand.agent.changes.prepare import closest_to, conflict_key
from errand.clock import IST
from errand.domain.brief import Brief, BudgetFact, ErrandKind, Provenance, ServiceFact
from errand.world.contracts import Area, BookingItem, Service, Slot

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
BOOKING = BookingItem.model_validate(
    {
        "booking_id": "B-77",
        "provider_id": "E4",
        "provider_name": "Venkatesh Electric Works",
        "service": Service.ELECTRICIAN,
        "slot": Slot(start=NOW - timedelta(days=19), end=NOW - timedelta(days=19, hours=-1)),
        "status": "completed",
    }
)
MESSAGE = "Book the same electrician as last time, but not before 10 a.m."


def pin(quote: str | None, brief: Brief | None = None, **kw: object) -> brief_ops.PinOutcome:
    return brief_ops.pin_from_history(
        brief or Brief(),
        booking=BOOKING,
        provider_area=Area.INDIRANAGAR,
        quote=quote,
        message_text=MESSAGE,
        message_id="m1",
        turn=1,
        **kw,  # type: ignore[arg-type]
    )


def test_a_history_reference_pins_the_provider_with_its_service_and_area() -> None:
    out = pin("same electrician as last time")
    assert out.applied and sorted(f.value for f in out.set_fields) == [
        "area",
        "provider_pref",
        "service",
    ]
    brief = out.brief
    assert brief.service is not None and brief.service.value is Service.ELECTRICIAN
    assert brief.area is not None and brief.area.value is Area.INDIRANAGAR
    assert brief.provider_pref is not None and brief.provider_pref.value.prefer_id == "E4"
    assert brief.provider_pref.prov.source == "booking_history"
    assert brief.provider_pref.prov.booking_id == "B-77"
    assert brief.budget_max_inr is None and brief.version == 1


@pytest.mark.parametrize("quote", ["last time", "same electrician as last time", "again"])
def test_each_history_phrase_counts(quote: str) -> None:
    out = brief_ops.pin_from_history(
        Brief(),
        booking=BOOKING,
        provider_area=Area.INDIRANAGAR,
        quote=quote,
        message_text=f"Please book an electrician, {quote}",
        message_id="m",
        turn=1,
    )
    assert out.applied, out.errors


def test_the_providers_name_counts_as_a_reference() -> None:
    out = brief_ops.pin_from_history(
        Brief(),
        booking=BOOKING,
        provider_area=Area.INDIRANAGAR,
        quote="Venkatesh Electric Works",
        message_text="Book Venkatesh Electric Works please",
        message_id="m",
        turn=1,
    )
    assert out.applied


@pytest.mark.parametrize(
    ("quote", "why"),
    [
        (None, "no evidence quote"),
        ("the same electrician", "carries no reference to a past booking"),
        ("the cheapest one", "not in the user's message"),
        ("ago", "shorter than 4"),
    ],
)
def test_a_pin_needs_the_users_words(quote: str | None, why: str) -> None:
    out = pin(quote)
    assert not out.applied and why in out.errors[0] and out.brief == Brief()


def test_a_booking_of_another_service_cannot_pin_a_request_for_a_plumber() -> None:
    plumber = Brief(
        service=ServiceFact(
            value=Service.PLUMBER, prov=Provenance(source="user_msg"), set_at_turn=1
        )
    )
    out = pin("same electrician as last time", plumber)
    assert not out.applied and "electrician" in out.errors[0]


def test_a_remembered_budget_plugs_in_through_budget_source_and_is_never_invented() -> None:
    assert pin("last time").brief.budget_max_inr is None
    prov = Provenance(source="memory", memory_id="m-9")
    fact = BudgetFact(value=700, prov=prov, set_at_turn=1)
    out = pin("last time", budget_source=lambda service: fact)
    assert out.brief.budget_max_inr == fact


def test_a_pinned_provider_needs_no_budget_but_a_plain_booking_does() -> None:
    pinned = pin("last time").brief
    assert brief_ops.pinned(pinned) == "E4"
    assert [f.value for f in brief_ops.missing(pinned, ErrandKind.BOOK)] == ["window"]
    plain = Brief(service=pinned.service, area=pinned.area, provider_pref=None)
    assert [f.value for f in brief_ops.missing(plain, ErrandKind.BOOK)] == [
        "window",
        "budget_max_inr",
    ]


def test_a_reschedule_needs_a_target_and_the_new_day() -> None:
    assert [f.value for f in brief_ops.missing(Brief(), ErrandKind.RESCHEDULE)] == [
        "target_booking_id",
        "window",
    ]


# ------------------------------------------------------------------ the card's slot


def slot(day: int, hour: int) -> Slot:
    start = datetime(2026, 11, day, hour, 0, tzinfo=IST)
    return Slot(start=start, end=start + timedelta(hours=1))


def test_the_card_takes_the_earliest_day_and_the_time_closest_to_the_original() -> None:
    original = datetime(2026, 11, 12, 11, 0, tzinfo=IST)
    slots = [slot(14, 7), slot(14, 13), slot(14, 17), slot(15, 11)]
    assert closest_to(slots, original, IST).start.hour == 13
    # a tie goes to the earlier slot
    assert closest_to([slot(14, 10), slot(14, 12)], original, IST).start.hour == 10
    # the earliest day wins over a closer time on a later day
    assert closest_to([slot(14, 17), slot(15, 11)], original, IST).start.day == 14


def test_a_refused_slot_has_its_own_key() -> None:
    assert conflict_key("A2", slot(14, 14).start) == "A2@2026-11-14T14:00:00+05:30"
