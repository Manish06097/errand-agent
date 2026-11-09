"""The replacement brief (Gate 3 decisions 8 and 10), its one-field widenings (13), and the
follow-up shortlist: the normal filters and ranking, with the cancelling provider last."""

from __future__ import annotations

import json
from datetime import datetime, time

from errand.clock import IST
from errand.domain.brief import Brief, BudgetFact, Provenance, ServiceFact, TimeWindow, WindowFact
from errand.followup.brief import replacement_brief, widened
from errand.followup.search import Built, Interval, build_shortlist
from errand.store.repos.known_bookings import KnownBooking
from errand.world.contracts import (
    Area,
    Attributes,
    ProviderSummary,
    SearchProvidersOut,
    Service,
    Slot,
)

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
USER = Provenance(source="user_msg")


def known(brief: Brief | None) -> KnownBooking:
    return KnownBooking(
        "B-1",
        "u_demo",
        "P3",
        "plumber",
        1,
        2,
        "cancelled_by_provider",
        0,
        "c_0001",
        brief.model_dump_json() if brief else None,
        "in_1",
    )


def original() -> Brief:
    window = TimeWindow(
        start=datetime(2026, 11, 9, tzinfo=IST),
        end=datetime(2026, 11, 16, tzinfo=IST),
        daily_from=time(7),
        daily_to=time(12),
        label="this week, mornings",
    )
    return Brief(
        version=3,
        service=ServiceFact(value=Service.PLUMBER, prov=USER, set_at_turn=1),
        window=WindowFact(value=window, prov=USER, set_at_turn=1),
        budget_max_inr=BudgetFact(value="none", prov=USER, set_at_turn=1),
    )


INTERVAL = Interval(datetime(2026, 11, 9, 10, tzinfo=IST), datetime(2026, 11, 13, tzinfo=IST))


def test_the_origin_brief_is_cloned_with_the_new_window_and_the_day_part_kept() -> None:
    b = replacement_brief(
        known(original()), interval=INTERVAL, tz=IST, now=INTERVAL.start, approved_price=300
    )
    assert b.service is not None and b.service.prov.source == "user_msg"  # stated, kept
    assert b.window is not None and b.window.value.start == INTERVAL.start
    assert (b.window.value.daily_from, b.window.value.daily_to) == (time(7), time(12))
    assert b.target_booking_id is not None and b.target_booking_id.value == "B-1"
    assert b.version == 1 and b.asks == []


def test_a_budget_of_none_falls_through_to_the_approved_price() -> None:
    b = replacement_brief(
        known(original()), interval=INTERVAL, tz=IST, now=INTERVAL.start, approved_price=300
    )
    assert b.budget_max_inr is not None and b.budget_max_inr.value == 300
    none = replacement_brief(
        known(original()), interval=INTERVAL, tz=IST, now=INTERVAL.start, approved_price=None
    )
    assert none.budget_max_inr is None


def test_a_booking_with_no_origin_brief_is_built_from_the_booking_alone() -> None:
    b = replacement_brief(
        known(None), interval=INTERVAL, tz=IST, now=INTERVAL.start, approved_price=None
    )
    assert b.service is not None and b.service.value == "plumber"
    assert b.service.prov.source == "followup"
    assert b.area is None  # no area known: the notice asks where to look
    assert b.window is not None and b.window.value.daily_from is None


def test_each_widening_changes_one_field_and_bumps_the_version() -> None:
    b = replacement_brief(
        known(original()), interval=INTERVAL, tz=IST, now=INTERVAL.start, approved_price=300
    )
    days = widened(b, "days", None, tz=IST, booking_id="B-1")
    assert days.window is not None and b.window is not None
    assert (days.window.value.end - b.window.value.end).days == 7
    assert days.budget_max_inr == b.budget_max_inr and days.version == b.version + 1
    time_ = widened(b, "time", None, tz=IST, booking_id="B-1")
    assert time_.window is not None and time_.window.value.daily_from is None
    assert time_.window.value.end == b.window.value.end
    price = widened(b, "price", "450", tz=IST, booking_id="B-1")
    assert price.budget_max_inr is not None and price.budget_max_inr.value == 450
    assert price.budget_max_inr.prov.source == "ui_action"
    assert price.window == b.window
    assert widened(b, "price", "abc", tz=IST, booking_id="B-1") == b  # not a number: no patch


def provider(pid: str, price: int, rating: float, hour: int = 10) -> ProviderSummary:
    slot = Slot(
        start=datetime(2026, 11, 10, hour, tzinfo=IST),
        end=datetime(2026, 11, 10, hour + 1, tzinfo=IST),
    )
    return ProviderSummary(
        provider_id=pid,
        name=f"Prov {pid}",
        service=Service.PLUMBER,
        area=Area.INDIRANAGAR,
        price_inr=price,
        rating=rating,
        review_count=0,
        available_slots=[slot],
        attributes=Attributes(verified=False, years_experience=1, languages=[], brands=[]),
    )


def window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 11, 10, tzinfo=IST), end=datetime(2026, 11, 11, tzinfo=IST), label="x"
    )


def built(ceiling: int | None, avoid: frozenset[str] = frozenset()) -> Built:
    result = SearchProvidersOut(
        providers=[provider("P3", 300, 4.9), provider("P9", 450, 4.0), provider("P5", 350, 3.5)]
    )
    return build_shortlist(
        result,
        window=window(),
        ceiling=ceiling,
        avoid=avoid,
        cancelling_provider="P3",
        brief_version=1,
        tz=IST,
    )


def test_the_cancelling_provider_sorts_after_every_other_fit_and_is_not_excluded() -> None:
    order = [c.provider_id for c in built(None).shortlist.candidates]
    assert order[-1] == "P3" and set(order) == {"P3", "P9", "P5"}


def test_the_ceiling_is_a_hard_filter_and_names_the_cheapest_that_is_over_it() -> None:
    b = built(300)
    assert [c.provider_id for c in b.shortlist.candidates] == ["P3"]
    assert b.cheapest_over_ceiling == 350
    assert built(None).cheapest_over_ceiling is None


def test_an_avoided_provider_is_excluded_with_its_reason() -> None:
    b = built(None, avoid=frozenset({"P9"}))
    assert "P9" not in [c.provider_id for c in b.shortlist.candidates]
    assert [(e.provider_id, e.reason) for e in b.shortlist.excluded] == [("P9", "avoided")]


def test_the_shortlist_is_json_serialisable_for_the_checkpoint() -> None:
    json.loads(built(None).shortlist.model_dump_json())
