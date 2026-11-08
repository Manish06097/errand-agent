"""The claim guard's lexical layer (10.8): claims matched to records by verb and object."""

from __future__ import annotations

from datetime import datetime

from errand.agent.claim_guard import Verified, guard
from errand.clock import IST

THURSDAY = datetime(2026, 11, 12, 9, 0, tzinfo=IST)
PLUMBER = Verified("B-5521", "P1", "Ravi Plumbing", "plumber", THURSDAY, this_run=True)
LATER = Verified("B-5521", "P1", "Ravi Plumbing", "plumber", THURSDAY, this_run=False)


def kept(text: str, *records: Verified, names: dict[str, str] | None = None) -> str:
    return guard(text, records, IST, provider_names=names).text


def test_paraphrases_and_hinglish_are_claims_with_nothing_behind_them() -> None:
    for text in [
        "You're all set for Thursday with Ravi.",
        "It's locked in.",
        "Sorted!",
        "Ravi will come at 9 on Thursday.",
        "Booking ho gaya.",
        "Done, it's booked.",
    ]:
        assert kept(text) == "", text


def test_a_true_claim_by_id_passes_in_a_later_run() -> None:
    assert kept("Your booking B-5521 is confirmed.", LATER) == "Your booking B-5521 is confirmed."


def test_a_cross_object_claim_fails_beside_a_true_one() -> None:
    text = "Your plumber B-5521 is confirmed. And I've booked the electrician too."
    assert kept(text, PLUMBER) == "Your plumber B-5521 is confirmed."


def test_the_wrong_day_or_an_unbooked_provider_fails() -> None:
    names = {"P2": "Shiva Plumbing Works"}
    assert kept("You're all set for Friday.", PLUMBER) == ""
    assert kept("Shiva is booked for Thursday.", PLUMBER, names=names) == ""
    assert kept("Ravi is booked for Thursday.", PLUMBER, names=names) != ""


def test_a_bare_claim_needs_an_outcome_verified_in_this_run() -> None:
    assert kept("All done!", PLUMBER) == "All done!"
    assert kept("All done!", LATER) == ""


def test_negated_and_conditional_sentences_are_not_claims() -> None:
    for text in [
        "Nothing is booked yet.",
        "It isn't confirmed until you tap the button.",
        "Once you approve, it will be booked.",
        "I can't book anything myself.",
    ]:
        assert kept(text) == text
