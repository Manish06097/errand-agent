"""Shapes on the wire, date_range validation, the catalog and the injection reviews."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest

from errand.world.catalog import BANGALORE_V1, INJECTION_REVIEWS
from errand.world.contracts import (
    Area,
    GetProviderError,
    SearchProvidersError,
    SearchProvidersOut,
    Service,
)
from errand.world.scenario import ScenarioConfig
from tests.contract.conftest import Env
from tests.contract.helpers import WEEK, body, provider, raw

SEARCH_KEYS = {
    "provider_id", "name", "service", "area", "price_inr", "rating", "review_count",
    "available_slots", "attributes",
}  # fmt: skip
PROVIDER_KEYS = {
    "provider_id", "name", "service", "area", "price_inr", "rating", "attributes", "about",
    "available_slots", "reviews",
}  # fmt: skip


def search_args(
    service: str = "plumber", area: str = "indiranagar", **range_: str
) -> dict[str, object]:
    return {
        "service": service,
        "area": area,
        "date_range": {
            "from": range_.get("frm", "2026-11-09"),
            "to": range_.get("to", "2026-11-15"),
        },
    }


async def test_search_shape_and_prices_are_integer_rupees(env: Env) -> None:
    client = await env.load()
    status, data = await raw(client, "search_providers", search_args())
    assert status == 200
    providers = body(data)["providers"]
    assert 3 <= len(providers) <= 8
    for p in providers:
        assert set(p) == SEARCH_KEYS
        assert isinstance(p["price_inr"], int)
        assert set(p["attributes"]) == {"verified", "years_experience", "languages", "brands"}
        for slot in p["available_slots"]:
            assert set(slot) == {"start", "end"}
            assert slot["start"].endswith("+05:30") and slot["end"].endswith("+05:30")
            assert "2026-11-09" <= slot["start"][:10] <= "2026-11-15"


@pytest.mark.parametrize(
    ("frm", "to", "ok"),
    [
        ("2026-11-09", "2026-11-09", True),  # one day
        ("2026-11-09", "2026-11-22", True),  # 14 days inclusive
        ("2026-11-09", "2026-11-23", False),  # 15 days
        ("2026-11-15", "2026-11-09", False),  # reversed
        ("2026-11-09T00:00:00+05:30", "2026-11-10", False),  # not a calendar date
    ],
)
async def test_date_range_validation_on_the_wire(env: Env, frm: str, to: str, ok: bool) -> None:
    client = await env.load()
    status, data = await raw(client, "search_providers", search_args(frm=frm, to=to))
    assert status == 200
    if ok:
        assert "providers" in body(data)
    else:
        answer = body(data)
        assert answer["error"] == "invalid_args"
        assert set(answer) == {"error", "reason"}


async def test_uncovered_area_is_invalid_args(env: Env) -> None:
    client = await env.load()
    _, data = await raw(client, "search_providers", search_args(area="hsr_layout"))
    assert body(data)["error"] == "invalid_args"


async def test_slot_with_another_offset_is_rejected(env: Env) -> None:
    client = await env.load()
    details = await provider(client, "P3")
    slot = details.available_slots[0]
    # The same instants written with a UTC offset: still not on contract.
    shifted = {
        "start": slot.start.astimezone(UTC).isoformat(),
        "end": slot.end.astimezone(UTC).isoformat(),
    }
    _, data = await raw(client, "hold_slot", {"provider_id": "P3", "slot": shifted})
    assert body(data)["error"] == "invalid_args"


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 11, 42])
async def test_every_covered_combo_lists_three_to_eight_providers(env: Env, seed: int) -> None:
    client = await env.load(ScenarioConfig(id="combos", seed=seed))
    for service, area in BANGALORE_V1:
        result = await client.search_providers(service, area, WEEK)
        assert isinstance(result, SearchProvidersOut)
        assert 3 <= len(result.providers) <= 8, (service, area, seed)
        assert {p.service for p in result.providers} == {service}
        assert {p.area for p in result.providers} == {area}


async def test_fixed_catalog_facts(env: Env) -> None:
    client = await env.load()
    plumbers = await client.search_providers(Service.PLUMBER, Area.INDIRANAGAR, WEEK)
    assert isinstance(plumbers, SearchProvidersOut)
    assert {"P3", "P9"} <= {p.provider_id for p in plumbers.providers}

    electricians = await client.search_providers(Service.ELECTRICIAN, Area.INDIRANAGAR, WEEK)
    assert isinstance(electricians, SearchProvidersOut)
    assert "E4" not in {p.provider_id for p in electricians.providers}
    e4 = await provider(client, "E4")  # absent from search, still answers get_provider
    assert (e4.service, e4.area) == (Service.ELECTRICIAN, Area.INDIRANAGAR)

    a2 = await provider(client, "A2")
    saturday = [s for s in a2.available_slots if s.start.date().isoformat() == "2026-11-14"]
    assert saturday, "A2 must have Saturday 14 Nov slots"

    now = await env.inspector.now()
    for p in (e4, a2):
        for slot in p.available_slots:
            assert slot.start > now
            assert slot.start.minute == 0 and slot.start.second == 0
            assert slot.end - slot.start == timedelta(hours=1)
    days = {s.start.date() for s in a2.available_slots}
    assert len(days) == 14


async def test_unknown_provider_is_not_found(env: Env) -> None:
    client = await env.load()
    assert await client.get_provider("P999") == GetProviderError(error="not_found")


async def test_injection_reviews_are_seeded_and_unmarked_on_the_wire(env: Env) -> None:
    client = await env.load()
    seen = 0
    for pid in sorted({pid for pid, _, _ in INJECTION_REVIEWS}):
        status, data = await raw(client, "get_provider", {"provider_id": pid})
        assert status == 200
        assert b"is_injection" not in data
        answer = body(data)
        assert set(answer) == PROVIDER_KEYS
        for review in answer["reviews"]:
            assert set(review) == {"review_id", "rating", "text", "posted_at"}
        texts = {r["text"] for r in answer["reviews"]}
        expected = {text for p, _, text in INJECTION_REVIEWS if p == pid}
        assert expected <= texts
        seen += len(expected)
    assert seen == len(INJECTION_REVIEWS) == 5
    zero_width = [t for _, _, t in INJECTION_REVIEWS if "​" in t]
    assert zero_width, "one injection hides behind zero-width characters"


async def test_search_error_shape(env: Env) -> None:
    client = await env.load()
    _, data = await raw(client, "search_providers", {"service": "plumber"})
    answer = SearchProvidersError.model_validate_json(data)
    assert answer.error == "invalid_args"
