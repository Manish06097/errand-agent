"""The pure parsers behind value-bound evidence (10.7), as small tables."""

from __future__ import annotations

from datetime import time

import pytest

from errand.agent import evidence


@pytest.mark.parametrize(
    ("span", "amounts"),
    [
        ("under ₹1,500", {1500}),
        ("under rs 1500", {1500}),
        ("Rs. 1,500/-", {1500}),
        ("inr 2000", {2000}),
        ("under rs.1500", {1500}),
        ("rs1500", {1500}),
        ("rs.1500", {1500}),
        ("inr1500", {1500}),
        ("1500rs", {1500}),
        ("1,500rs", {1500}),
        ("1500 rs.", {1500}),
        ("₹1500", {1500}),
        ("1.5k rs", {1500}),
        ("1.5k", {1500}),
        ("2k max", {2000}),
        ("around 1,500 rupees", {1500}),
        ("1,50,000", {150000}),
        ("2 lakh", {200000}),
        ("fifteen hundred", {1500}),
        ("15 hundred", {1500}),
        ("one thousand five hundred", {1500}),
        ("a thousand", {1000}),
        ("twelve hundred rupees", {1200}),
        ("two thousand and five hundred", {2500}),
        ("between 1000 and 1500", {1000, 1500}),
        ("mornings only", set()),
        ("kitchen tap", set()),
        ("no one", set()),
        ("thursday 12", {12}),
    ],
)
def test_amounts(span: str, amounts: set[int]) -> None:
    assert evidence.parse_amounts(span) == amounts


@pytest.mark.parametrize(
    "span",
    ["no budget", "any price is fine", "it doesn't matter", "no limit", "price does not matter"],
)
def test_no_limit_phrases(span: str) -> None:
    assert evidence.has_no_limit(span)


@pytest.mark.parametrize("span", ["under 1500", "budget is fine", "a limit of 800"])
def test_not_a_no_limit_phrase(span: str) -> None:
    assert not evidence.has_no_limit(span)


@pytest.mark.parametrize(
    ("span", "times"),
    [
        ("not before 10 a.m.", {time(10)}),
        ("10am", {time(10)}),
        ("after 5 pm", {time(17)}),
        ("5:30 p.m.", {time(17, 30)}),
        ("12 am", {time(0)}),
        ("12 pm", {time(12)}),
        ("17:00", {time(17)}),
        ("07:30", {time(7, 30)}),
        ("by 7.30", {time(7, 30), time(19, 30)}),
        ("7.30 pm", {time(19, 30)}),
        ("at 10.15 am", {time(10, 15)}),
        ("after 5", {time(5), time(17)}),
        ("not before ten", {time(10), time(22)}),
        ("ten am", {time(10)}),
        ("noon", {time(12)}),
        ("10:30", {time(10, 30), time(22, 30)}),
        ("under ₹1,500", set()),
        ("1500", set()),
        ("25:00", set()),
        ("13 pm", set()),
    ],
)
def test_times(span: str, times: set[time]) -> None:
    assert evidence.parse_times(span) == times


def test_words_in_an_issue() -> None:
    assert evidence.words_in("kitchen tap", "my kitchen tap is leaking")
    assert evidence.words_in("leaking tap", "the tap is leaking")
    assert evidence.words_in("taps", "kitchen tap")
    assert not evidence.words_in("bathroom geyser", "kitchen tap")
    assert not evidence.words_in("kitchen sink", "kitchen tap")
    # stop words and short words are not content
    assert evidence.words_in("the tap", "tap")
