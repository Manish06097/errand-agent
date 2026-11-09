"""The standing-preference cue table (gate 2 decision 3)."""

from __future__ import annotations

import pytest

from errand.memory.cues import check_span


@pytest.mark.parametrize(
    ("span", "key", "cue"),
    [
        ("I always want mornings", "earliest_time", "always"),
        ("Never book me before ten", "earliest_time", "never"),
        ("from now on, Indiranagar", "home_area", "from now on"),
        ("every time I book a plumber", "budget_ceiling", "every time"),
        ("I live in Whitefield", "home_area", "i live in"),
        ("I prefer Arjun Electricals", "preferred_provider", "i prefer"),
        ("my usual slot is 10", "earliest_time", "my usual"),
        ("I moved to Koramangala", "home_area", "i moved to"),
        ("I" + chr(0x2019) + "ve moved to Koramangala", "home_area", "i've moved to"),
        ("ALWAYS", "language", "always"),
    ],
)
def test_a_standing_cue_is_found(span: str, key: str, cue: str) -> None:
    result = check_span(span, key)
    assert result.ok and result.cue == cue


def test_the_moved_cue_belongs_to_home_area_only() -> None:
    assert check_span("I moved to Koramangala", "home_area").ok
    assert not check_span("I moved to 10 am", "earliest_time").ok


@pytest.mark.parametrize(
    "span",
    [
        "this time I prefer after 5",
        "I always want it today",
        "for now, never before ten",
        "just for this booking, I live in Whitefield",
    ],
)
def test_a_one_off_marker_in_the_same_span_vetoes_the_cue(span: str) -> None:
    result = check_span(span, "earliest_time")
    assert result.cue is not None and result.veto is not None and not result.ok


@pytest.mark.parametrize(
    "span",
    ["mornings only please", "book a plumber under 1500", "alwaysfree", "unaware", "nevermind"],
)
def test_no_cue_is_no_save(span: str) -> None:
    assert check_span(span, "earliest_time").cue is None


def test_a_cue_must_be_a_whole_phrase() -> None:
    assert check_span("the neverending story", "language").cue is None
