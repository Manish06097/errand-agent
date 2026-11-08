"""Ranking is a pure function (architecture 10.6), so it gets unit tests."""

from __future__ import annotations

import pytest

from errand.agent.ranking import WEIGHTS, RankInput, rank, review_signal, score_parts

FIVE_STARS = {"R-1": 5, "R-2": 5}


def test_the_weights_are_the_design_formula() -> None:
    assert dict(WEIGHTS) == {
        "rating": 0.35,
        "price_headroom": 0.25,
        "slot_fit": 0.15,
        "review_signal": 0.15,
        "pref_match": 0.10,
    }
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_review_signal_is_the_mean_of_clean_labelled_stars() -> None:
    assert review_signal({"a": 5, "b": 3}, {"a": False, "b": False}) == pytest.approx(0.75)


def test_one_flagged_review_zeroes_the_signal_whatever_the_rest_say() -> None:
    assert review_signal(FIVE_STARS, {"R-1": False, "R-2": True}) == 0.0


def test_unread_or_failed_reads_give_no_signal() -> None:
    assert review_signal(FIVE_STARS, None) == 0.0
    assert review_signal(FIVE_STARS, {}) == 0.0


def test_unlabelled_reviews_never_count() -> None:
    # R-2 has no label (the reader skipped it): only R-1's 1 star counts.
    assert review_signal({"R-1": 1, "R-2": 5}, {"R-1": False}) == 0.0


def test_a_flagged_review_can_only_lower_its_providers_rank() -> None:
    honest = RankInput("P9", 4.8, 450, 3, FIVE_STARS, {"R-1": False, "R-2": False})
    injected = RankInput("P9", 4.8, 450, 3, FIVE_STARS, {"R-1": False, "R-2": True})
    other = RankInput("P12", 4.3, 500, 3, {"R-3": 5}, {"R-3": False})
    with_honest = [r.provider_id for r in rank([honest, other], budget=1500)]
    with_injected = [r.provider_id for r in rank([injected, other], budget=1500)]
    assert with_honest == ["P9", "P12"]
    assert with_injected == ["P12", "P9"]


def test_parts_follow_their_definitions() -> None:
    c = RankInput("P1", 5.0, 600, 2)
    assert score_parts(c, budget=1200, cheapest=300) == {
        "rating": 1.0,
        "price_headroom": 0.5,
        "slot_fit": 0.6667,
        "review_signal": 0.0,
        "pref_match": 0.0,
    }
    # No budget: price relative to the cheapest candidate.
    assert score_parts(c, budget="none", cheapest=300)["price_headroom"] == 0.5
    assert score_parts(c, budget=None, cheapest=600)["price_headroom"] == 1.0


def test_ties_go_to_the_smaller_provider_id_and_the_order_is_stable() -> None:
    a = RankInput("P3", 4.0, 500, 3)
    b = RankInput("P10", 4.0, 500, 3)
    assert [r.provider_id for r in rank([a, b], budget=1000)] == ["P10", "P3"]
    assert [r.provider_id for r in rank([b, a], budget=1000)] == ["P10", "P3"]


def test_an_empty_shortlist_ranks_to_nothing() -> None:
    assert rank([], budget=None) == []
