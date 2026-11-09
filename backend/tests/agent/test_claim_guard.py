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


def test_a_negation_word_elsewhere_does_not_exempt_a_claim() -> None:
    """BUG-M4-2: the lexicon used to exempt any sentence holding a negation-like word."""
    for text in [
        "Your plumber is booked for Saturday, no worries.",
        "Booked, nothing else needed from you.",
        "All set, Ravi will come at 10, not to worry.",
        "I've booked Sharma Plumbing for you until further notice.",
        "No problem, it's booked for Saturday.",
        "If you're wondering, your plumber is booked for Saturday.",
        "Not only is it booked, it is confirmed.",
    ]:
        assert kept(text) == "", text
    assert kept("Booked! Nothing else needed from you.") == "Nothing else needed from you."


def test_negation_that_scopes_over_the_claim_verb_still_exempts() -> None:
    for text in [
        "It's not booked yet.",
        "I haven't booked anything.",
        "That isn't confirmed.",
        "Nothing has been booked.",
        "Once you approve, it will be booked.",
        "Once you approve, Ravi will come at 10.",
        "It will be booked after you tap the button.",
        "Booking pakka nahi hua abhi.",
        "Ho gaya nahi, abhi pending hai.",
    ]:
        assert kept(text) == text, text


def test_sorted_by_and_a_search_done_are_not_claims() -> None:
    assert kept("Here are three options, sorted by price.") != ""
    assert kept("I've done a search and found three plumbers.") != ""
    assert kept("I've done the search again.") != ""
    assert kept("I've done the booking.") == ""
    assert kept("It's sorted!") == ""
    assert kept("Sorted out, you're good.") == ""


# BUG-M4-2 round 5: a present claim under a present-tense approval may take a subject
# pronoun and a be-form ("you're all set"), a modal bridges only a provider name that is
# on the shortlist or among the user's known bookings, and the approval opener matches in
# any case. Sharma is on the shortlist; Ramesh is on neither list.
SHORTLIST = {"P7": "Sharma Plumbing"}
ROUND5_KEPT = [
    "Once you approve it gets booked.",
    "Once you approve, you're all set.",
    "Once you approve, you're booked.",
    "Once you approve, we're all set.",
    "Once you approve, you are all set.",
    "Once you approve, they're booked.",
    "If you want, I can get Sharma booked for Saturday.",
    "If you want, I can get Sharma Plumbing booked for Saturday.",
    "Once You Approve, I Can Get Sharma Booked.",
]
ROUND5_REMOVED = [
    "I can get Saturday booked.",
    "If you want, I can get Ramesh booked for Saturday.",
    "If you want, I can get Saturday booked.",
    "Once You Approve You Can See It Is Booked.",
    "Once you approved, you're all set.",
    "Once you refresh, you're all set.",
    "Once you approve, you're all set, and I booked Sharma.",
    "Once you approve, you were booked.",
    "You're all set.",
    "Once you approve, you're told I booked Sharma.",
]


def test_round5_the_guard_keeps_the_honest_lines_and_removes_the_claims() -> None:
    for text in ROUND5_KEPT:
        assert kept(text, names=SHORTLIST) == text, text
    for text in ROUND5_REMOVED:
        assert kept(text, names=SHORTLIST) == "", text


def test_round5_a_name_bridges_only_from_the_shortlist_or_a_known_booking() -> None:
    line = "If you want, I can get Ramesh booked for Saturday."
    assert kept(line, names=SHORTLIST) == ""
    assert kept(line) == ""
    # A known booking's provider is a name too, whether or not it is on the shortlist.
    ramesh = Verified("B-9", "P9", "Ramesh Electricals", "electrician", THURSDAY)
    assert kept(line, ramesh, names=SHORTLIST) == line
    # Sharma is only a name when the caller says so: the model's own text never makes one.
    assert kept("If you want, I can get Sharma booked for Saturday.") == ""
