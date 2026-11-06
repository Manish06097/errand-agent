"""Wire shapes that are pure value rules: Slot, date_range, the hold predicate."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from errand.clock import IST
from errand.world.contracts import (
    SPECS,
    BookOut,
    DateRange,
    GetBookingStatusIn,
    ProviderSummary,
    Slot,
    encode,
    hold_is_live,
)

START = datetime(2026, 11, 13, 9, 0, tzinfo=IST)


def test_slot_round_trips_with_the_ist_offset() -> None:
    slot = Slot(start=START, end=START + timedelta(hours=1))
    assert (
        encode(slot) == b'{"start":"2026-11-13T09:00:00+05:30","end":"2026-11-13T10:00:00+05:30"}'
    )
    assert Slot.model_validate_json(encode(slot)) == slot


@pytest.mark.parametrize(
    "start",
    ["2026-11-13T03:30:00+00:00", "2026-11-13T09:00:00", "2026-11-13T09:00:00+05:00"],
)
def test_slot_rejects_any_other_offset(start: str) -> None:
    with pytest.raises(ValidationError):
        Slot.model_validate_json(f'{{"start": "{start}", "end": "2026-11-13T10:00:00+05:30"}}')


def test_slot_end_after_start() -> None:
    with pytest.raises(ValidationError):
        Slot(start=START, end=START)


@pytest.mark.parametrize(("days", "ok"), [(1, True), (7, True), (14, True), (15, False)])
def test_date_range_is_at_most_14_days_inclusive(days: int, ok: bool) -> None:
    frm = date(2026, 11, 9)
    raw = {"from": frm, "to": frm + timedelta(days=days - 1)}
    if ok:
        assert DateRange.model_validate(raw).to == raw["to"]
    else:
        with pytest.raises(ValidationError, match="at most 14"):
            DateRange.model_validate(raw)


def test_date_range_reversed_and_wire_alias() -> None:
    with pytest.raises(ValidationError, match="after"):
        DateRange.model_validate({"from": date(2026, 11, 10), "to": date(2026, 11, 9)})
    week = DateRange.model_validate_json('{"from": "2026-11-09", "to": "2026-11-15"}')
    assert encode(week) == b'{"from":"2026-11-09","to":"2026-11-15"}'


def test_hold_is_live_strictly_before_expiry() -> None:
    expires = START + timedelta(minutes=10)
    assert hold_is_live(expires - timedelta(milliseconds=1), expires)
    assert not hold_is_live(expires, expires)
    assert not hold_is_live(expires + timedelta(milliseconds=1), expires)
    # Offsets do not matter, instants do.
    assert not hold_is_live(expires.astimezone(UTC), expires)


def test_every_wire_model_forbids_extra_fields_and_is_strict() -> None:
    for spec in SPECS.values():
        for model in (spec.input, spec.ok, spec.error):
            assert model.model_config.get("extra") == "forbid", model
            assert model.model_config.get("strict") is True, model
    good = (
        '{"status":"confirmed","booking_id":"B-1","provider_id":"P3","price_inr":450,'
        '"slot":{"start":"2026-11-13T09:00:00+05:30","end":"2026-11-13T10:00:00+05:30"}'
    )
    assert BookOut.model_validate_json(good + "}").price_inr == 450
    with pytest.raises(ValidationError):
        BookOut.model_validate_json(good + ',"note":"x"}')
    with pytest.raises(ValidationError):
        BookOut.model_validate_json(good.replace("450", '"450"') + "}")
    with pytest.raises(ValidationError):
        BookOut.model_validate_json(good.replace("450", "450.5") + "}")


def test_status_lookup_takes_exactly_one_form() -> None:
    assert GetBookingStatusIn(hold_id="H-1").hold_id == "H-1"
    assert GetBookingStatusIn(idempotency_key="k").idempotency_key == "k"
    with pytest.raises(ValidationError):
        GetBookingStatusIn()
    with pytest.raises(ValidationError):
        GetBookingStatusIn(hold_id="H-1", idempotency_key="k")


def test_provider_summary_has_no_review_or_eval_fields() -> None:
    assert "is_injection" not in ProviderSummary.model_fields
    assert "reviews" not in ProviderSummary.model_fields
