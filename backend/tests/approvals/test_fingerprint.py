"""Fingerprint canonicalisation (8.1): one of the few places a unit test is the proof.

Equivalent inputs give the same hash (an IST time and its UTC twin, nearby ids in any
order or repeated, a datetime or its ISO string); any change to a material field gives
a different hash; the schema version is part of it; `hold_id` is not material at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from errand.approvals.fingerprint import canonical_json, fingerprint, parse_material
from errand.clock import IST
from errand.domain.material import BookMaterial, ist_seconds
from errand.world.contracts import Service

BASE = datetime(2026, 11, 10, 9, 0, tzinfo=IST)
starts = st.integers(min_value=0, max_value=14 * 24).map(lambda h: BASE + timedelta(hours=h))
ids = st.lists(st.from_regex(r"B-[0-9]{3,5}", fullmatch=True), max_size=4)
prices = st.integers(min_value=100, max_value=10_000)
providers = st.sampled_from(["P1", "P2", "P3", "E4", "A2"])
services = st.sampled_from(list(Service))


def book(
    start: datetime,
    *,
    provider: str = "P1",
    service: Service = Service.PLUMBER,
    price: int = 1350,
    nearby: list[str] | None = None,
    end: datetime | None = None,
) -> BookMaterial:
    return BookMaterial(
        user_id="u_demo",
        provider_id=provider,
        service=service,
        slot_start=ist_seconds(start),
        slot_end=ist_seconds(end or start + timedelta(hours=1)),
        quoted_price_inr=price,
        same_service_nearby=nearby or [],
    )


@given(start=starts, nearby=ids, price=prices, provider=providers, service=services)
def test_equivalent_inputs_give_the_same_hash(
    start: datetime, nearby: list[str], price: int, provider: str, service: Service
) -> None:
    a = book(start, provider=provider, service=service, price=price, nearby=nearby)
    # The same instant in UTC, the ids reversed and repeated, the time as an ISO string.
    b = BookMaterial(
        user_id="u_demo",
        provider_id=provider,
        service=service,
        slot_start=start.astimezone(UTC).isoformat(),
        slot_end=(start + timedelta(hours=1)).astimezone(UTC),  # type: ignore[arg-type]
        quoted_price_inr=price,
        same_service_nearby=list(reversed(nearby)) + nearby,
    )
    assert canonical_json(a) == canonical_json(b)
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(parse_material(a.model_dump_json())) == fingerprint(a)


@given(
    start=starts,
    price=prices,
    field=st.sampled_from(["provider", "service", "start", "end", "price", "nearby", "user"]),
)
def test_any_material_change_gives_a_different_hash(
    start: datetime, price: int, field: str
) -> None:
    a = book(start, price=price)
    changes = {
        "provider": lambda: book(start, provider="P2", price=price),
        "service": lambda: book(start, service=Service.ELECTRICIAN, price=price),
        "start": lambda: book(
            start + timedelta(minutes=30), price=price, end=start + timedelta(hours=1)
        ),
        "end": lambda: book(start, price=price, end=start + timedelta(hours=2)),
        "price": lambda: book(start, price=price + 1),
        "nearby": lambda: book(start, price=price, nearby=["B-1"]),
        "user": lambda: a.model_copy(update={"user_id": "u_other"}),
    }
    assert fingerprint(changes[field]()) != fingerprint(a)


def test_times_are_ist_with_seconds_and_money_is_an_integer() -> None:
    m = book(datetime(2026, 11, 10, 3, 30, 15, 999_000, tzinfo=UTC))
    assert m.slot_start == "2026-11-10T09:00:15+05:30"
    with pytest.raises(ValidationError):
        BookMaterial(
            user_id="u",
            provider_id="P1",
            service=Service.PLUMBER,
            slot_start=m.slot_start,
            slot_end=m.slot_end,
            quoted_price_inr=1350.0,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="offset"):
        BookMaterial.model_validate(
            {**m.model_dump(), "slot_start": "2026-11-10T09:00:00"}  # naive
        )


def test_the_schema_version_is_hashed_and_hold_id_is_not_material() -> None:
    m = book(BASE)
    assert '"v":1' in canonical_json(m)
    assert "hold" not in canonical_json(m)
    assert fingerprint(m).startswith("fp1_")
    with pytest.raises(ValidationError):
        BookMaterial.model_validate({**m.model_dump(), "hold_id": "H-1"})
