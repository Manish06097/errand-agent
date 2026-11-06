"""Catalog invariants that hold for every seed, checked over many more seeds than the
contract suite can afford to load."""

from __future__ import annotations

from datetime import date

from errand.world.catalog import (
    BANGALORE_V1,
    FIRST_HOUR,
    LAST_START_HOUR,
    MIN_FREE_PER_DAY,
    free_hours,
    listed_ids,
    provider_row,
)
from errand.world.contracts import Area


def test_every_combo_lists_three_to_eight_for_every_seed() -> None:
    for seed in range(1000):
        for roster in BANGALORE_V1.values():
            assert 3 <= len(listed_ids(seed, roster)) <= 8


def test_ids_are_unique_and_named_providers_are_where_scenarios_expect() -> None:
    every = [pid for r in BANGALORE_V1.values() for pid in (*r.core, *r.optional, *r.unlisted)]
    assert len(every) == len(set(every))
    for service_area, roster in BANGALORE_V1.items():
        assert set(roster.core).isdisjoint(roster.optional)
        if service_area[1] == Area.INDIRANAGAR and service_area[0].value == "plumber":
            assert {"P2", "P3", "P9"} <= set(roster.core)
    unlisted = [pid for r in BANGALORE_V1.values() for pid in r.unlisted]
    assert unlisted == ["E4"]


def test_provider_identity_does_not_depend_on_the_seed() -> None:
    # Names, prices and ratings come from the template, so "P3" means the same
    # provider in every scenario.
    assert provider_row("P3", Area.INDIRANAGAR, True) == provider_row("P3", Area.INDIRANAGAR, True)
    row = provider_row("A2", Area.INDIRANAGAR, True)
    assert isinstance(row.base_price_inr, int) and row.base_price_inr % 50 == 0


def test_free_hours_are_on_the_hour_and_at_least_two_a_day() -> None:
    for seed in range(50):
        for day in (date(2026, 11, 9), date(2026, 11, 14)):
            hours = free_hours(seed, "A2", day)
            assert len(hours) >= MIN_FREE_PER_DAY
            assert all(FIRST_HOUR <= h <= LAST_START_HOUR for h in hours)
            assert hours == sorted(set(hours))
