"""Sanitizer patterns: what must be masked, and what must come through untouched."""

from __future__ import annotations

import pytest

from errand.trace.sanitize import ADDRESS_MASK, PHONE_MASK, sanitize, sanitize_text


@pytest.mark.parametrize(
    "number",
    [
        "080 4123 4567",
        "(080) 4123 4567",
        "(080) 4123-4567",
        "080-41234567",
        "08041234567",
        "0471-2345678",
        "0821 234567",
        "09845012345",
        "+91 98450 12345",
        "98450 12345",
    ],
)
def test_phone_formats_in_free_text_are_masked(number: str) -> None:
    assert sanitize_text(f"call {number}, ask for Ravi.") == f"call {PHONE_MASK}, ask for Ravi."


@pytest.mark.parametrize(
    "key",
    ["phone", "Phone", "phone_number", "mobile", "contact_number", "whatsapp", "alt_phone"],
)
def test_phone_like_keys_are_masked_whatever_the_value(key: str) -> None:
    for value in ("080 4123 4567", "ext. 22, ask at the desk", 8041234567, ["080 4123 4567"]):
        assert sanitize({key: value}) == {key: PHONE_MASK}
    assert sanitize({key: None}) == {key: None}


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-07T10:00:00.000+05:30",
        "2026-11-07",
        "1794025800000",
        "slot_P7_1794025800000",
        "cost 0.000123456789",
        "0800 hrs",
        "id 01234567",
        "price 0450",
        "Since 1998",
    ],
)
def test_timestamps_epoch_ms_ids_and_numbers_survive(text: str) -> None:
    assert sanitize_text(text) == text


def test_a_pincode_after_a_city_is_masked_with_or_without_a_street() -> None:
    assert sanitize_text("12 MG Road, Bengaluru 560038") == ADDRESS_MASK
    assert sanitize_text("Indiranagar, Bengaluru 560038") == f"Indiranagar, {ADDRESS_MASK}"
    assert "560038" not in sanitize_text("ship to Bengaluru, 560038 please")
