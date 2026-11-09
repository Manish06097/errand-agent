"""The registry: every key's parse, render and surface forms (gate 2 decision 2)."""

from __future__ import annotations

import pytest

from errand.memory.registry import (
    REGISTRY,
    RegistryError,
    check_scope,
    parse_value,
    render_value,
    spec_for,
    surface_forms,
)


def test_the_registry_has_exactly_the_nine_keys_of_the_gate() -> None:
    assert list(REGISTRY) == [
        "home_area",
        "earliest_time",
        "latest_time",
        "avoid_days",
        "budget_ceiling",
        "preferred_provider",
        "avoid_provider",
        "language",
        "access_note",
        "contact_window",
    ]
    assert {k for k, s in REGISTRY.items() if s.pinned} == {
        "home_area",
        "earliest_time",
        "latest_time",
    }


def test_an_unknown_key_and_a_wrong_scope_are_errors() -> None:
    with pytest.raises(RegistryError, match="unknown memory key"):
        spec_for("favourite_colour")
    with pytest.raises(RegistryError, match="global only"):
        check_scope(spec_for("home_area"), "service:plumber")
    with pytest.raises(RegistryError, match="per service"):
        check_scope(spec_for("budget_ceiling"), "global")
    with pytest.raises(RegistryError, match="scope must be"):
        check_scope(spec_for("earliest_time"), "service:Plumber!")


@pytest.mark.parametrize(
    ("key", "scope", "raw", "stored"),
    [
        ("home_area", "global", "Indira Nagar", "indiranagar"),
        ("earliest_time", "global", "10", "10:00"),
        ("earliest_time", "service:electrician", "10am", "10:00"),
        ("latest_time", "global", "5 p.m.", "17:00"),
        ("latest_time", "global", "12:00 am", "00:00"),
        ("avoid_days", "global", ["Sun", "saturday"], ["saturday", "sunday"]),
        ("budget_ceiling", "service:plumber", "₹1,500", 1500),
        ("budget_ceiling", "service:plumber", 800, 800),
        ("preferred_provider", "service:electrician", "p4", "P4"),
        ("avoid_provider", "service:plumber", ["P7", "P2"], ["P2", "P7"]),
        ("language", "global", "Hinglish", "hinglish"),
        (
            "contact_window",
            "global",
            {"from": "9", "to": "18:00"},
            {"from": "09:00", "to": "18:00"},
        ),
    ],
)
def test_values_are_stored_in_one_canonical_form(
    key: str, scope: str, raw: object, stored: object
) -> None:
    assert parse_value(key, scope, raw)[2] == stored


@pytest.mark.parametrize(
    ("key", "scope", "raw"),
    [
        ("home_area", "global", "HSR Layout"),
        ("earliest_time", "global", "25:00"),
        ("earliest_time", "global", "soon"),
        ("avoid_days", "global", ["funday"]),
        ("budget_ceiling", "service:plumber", "cheap"),
        ("budget_ceiling", "service:plumber", 50),
        ("budget_ceiling", "service:plumber", True),
        ("preferred_provider", "service:electrician", "Arjun"),
        ("avoid_provider", "service:plumber", []),
        ("language", "global", "klingon"),
        ("access_note", "global", "ok"),
        ("contact_window", "global", {"from": "18:00", "to": "09:00"}),
        ("contact_window", "global", "evenings"),
    ],
)
def test_badly_typed_values_are_registry_errors(key: str, scope: str, raw: object) -> None:
    with pytest.raises(RegistryError):
        parse_value(key, scope, raw)


def test_access_note_is_cleaned_like_world_text_and_capped() -> None:
    raw = "Ring​ the  bell twice.\n" + "x" * 300
    value = parse_value("access_note", "global", raw)[2]
    assert "​" not in value and "  " not in value and len(value) <= 120


def test_surface_forms_cover_the_ways_a_value_is_written() -> None:
    assert {"10:00", "10 am", "10am", "10 a.m.", "ten"} <= set(surface_forms("earliest_time", "10"))
    assert {"5 pm", "17:00", "five"} <= set(surface_forms("latest_time", "17:00"))
    assert {"indiranagar", "indira nagar"} <= set(surface_forms("home_area", "indiranagar"))
    assert {"₹1,500", "1500", "rs 1500"} <= set(surface_forms("budget_ceiling", 1500))
    assert {"saturday", "sat"} <= set(surface_forms("avoid_days", ["saturday"]))
    assert surface_forms("avoid_provider", ["P2", "P7"]) == ("P2", "P7")


def test_render_round_trips_the_stored_value() -> None:
    assert render_value("budget_ceiling", 800) == "₹800"
    assert render_value("contact_window", {"from": "09:00", "to": "18:00"}) == "09:00-18:00"
    assert render_value("avoid_provider", ["P2", "P7"]) == "P2, P7"
    with pytest.raises(RegistryError):
        render_value("home_area", "atlantis")
