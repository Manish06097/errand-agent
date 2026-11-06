"""Keyed draws: pure, stable, and distributed as the brief says (architecture 5.4)."""

from __future__ import annotations

from collections import Counter

import pytest

from errand.world.rng import BOOK_OUTCOMES, book_outcome, draw
from errand.world.scenario import BookProbabilities

PROBS = BookProbabilities().as_dict()


def test_draw_is_the_documented_sha256_function() -> None:
    # Pinned values: changing the derivation would silently reshuffle every scenario.
    assert draw(22, "book", "bo_7d", 0) == 0.9188396280988033
    assert draw(22, "book", "bo_7d", 0) == draw(22, "book", "bo_7d", 0)
    assert draw(22, "book", "bo_7d", 1) != draw(22, "book", "bo_7d", 0)
    assert draw(23, "book", "bo_7d", 0) != draw(22, "book", "bo_7d", 0)


def test_twenty_thousand_keyed_draws_match_the_brief_split() -> None:
    n = 20_000
    counts = Counter(book_outcome(draw(1, "book", f"key-{i}", 0), PROBS) for i in range(n))
    expected = {
        "success": 70.0,
        "slot_taken": 15.0,
        "timeout_booked": 7.5,
        "timeout_not_booked": 7.5,
    }
    for outcome in BOOK_OUTCOMES:
        share = 100 * counts[outcome] / n
        assert abs(share - expected[outcome]) <= 1.0, (outcome, share)


def test_book_outcome_bucket_edges() -> None:
    assert book_outcome(0.0, PROBS) == "success"
    assert book_outcome(0.6999, PROBS) == "success"
    assert book_outcome(0.70, PROBS) == "slot_taken"
    assert book_outcome(0.8499, PROBS) == "slot_taken"
    assert book_outcome(0.85, PROBS) == "timeout_booked"
    assert book_outcome(0.9249, PROBS) == "timeout_booked"
    assert book_outcome(0.925, PROBS) == "timeout_not_booked"
    assert book_outcome(0.999999, PROBS) == "timeout_not_booked"
    only_success = BookProbabilities(
        success=1.0, slot_taken=0.0, timeout_booked=0.0, timeout_not_booked=0.0
    ).as_dict()
    assert book_outcome(0.9999999999, only_success) == "success"
    with pytest.raises(ValueError):
        book_outcome(1.0, PROBS)
