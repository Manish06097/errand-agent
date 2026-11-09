"""BUG-M4-2: any negation-like word in a sentence exempted a false booking claim.

"Your plumber is booked for Saturday, no worries." reached the transcript with no
booking behind it, because the claim guard (and INV-04, with its own copy of the idea)
treated a sentence holding "no", "nothing", "until" and the like as not a claim at all.
The same lexicon removed harmless text ("sorted by price", "done a search").

Ruling: a negator exempts a claim only where it scopes over the claim verb. This runs the
hostile line end to end (S62: ParaphraseClaimPolicy says it while a card waits) and then
pins both layers separately.
"""

from __future__ import annotations

from pathlib import Path

from errand.agent.claim_guard import guard
from errand.clock import IST
from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario


async def test_the_hostile_line_never_reaches_the_transcript(tmp_path: Path) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S62_paraphrase_claim_policy.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures


def test_the_guard_removes_the_leaked_claims_and_keeps_the_harmless_text() -> None:
    claims = [
        "Your plumber is booked for Saturday, no worries.",
        "Booked, nothing else needed from you.",
        "All set, Ravi will come at 10, not to worry.",
        "I've booked Sharma Plumbing for you until further notice.",
    ]
    for text in claims:
        assert guard(text, [], IST).text == "", text
    harmless = ["Here are three options, sorted by price.", "I've done a search and found three."]
    for text in harmless:
        assert guard(text, [], IST).text == text


# Second fix (round 2): a negator that belongs to another verb ("don't worry", "no stress",
# "not a problem") or a conditional opener on a past-tense claim ("when you asked") still
# exempted a real claim, in both layers.
_ROUND2_LEAKS = [
    "Don't worry I booked Sharma Plumbing for you.",
    "No stress it's booked for Saturday.",
    "When you asked I booked it right away.",
    "Not a problem I have booked Ramesh for Saturday.",
    "Unless you object I have reserved Saturday 10am.",
]
_STILL_EXEMPT = [
    "I haven't booked anything yet.",
    "Nothing has been booked.",
    "It is not yet confirmed.",
    "Once you tap approve it will be booked.",
    "If you approve, I will book it.",
    "Once you approve it is booked.",
]


def test_round2_the_guard_keeps_no_leaked_claim_and_still_exempts_real_negation() -> None:
    for text in _ROUND2_LEAKS:
        assert guard(text, [], IST).text == "", text
    for text in _STILL_EXEMPT:
        assert guard(text, [], IST).text == text, text


# Third fix (round 3): a condition reached the claim across clause breaks and past-tense
# approval verbs, and an interjection "No" or "Nothing to do" reached it through pronouns
# and determiners. Both layers (the guard and INV-04) had the holes, with separate code.
ROUND3_LEAKS = [
    "Once you approved I booked Sharma Plumbing for Saturday.",
    "No the plumber is booked.",
    "Once you approve it will be booked, and I already booked Sharma for Saturday.",
    "If you need anything I can help, but I've booked Sharma for Saturday.",
    "When I checked, the slot could go fast, so I booked it.",
    "After you tapped approve, I booked Sharma Plumbing for Saturday.",
    "No I booked it already.",
    "Nothing to do it's booked.",
    "Nothing to do I have booked it.",
    "Once you tap approve I've booked Sharma for Saturday.",
    "Once you confirmed it is booked.",
    "Once the plumber confirms it will be booked, but I booked Ramesh for Saturday.",
    "If you approve it will be booked, so I already booked it.",
    "I have yet to book anything, but I booked it.",
    "Once you approve, nothing is booked, and I booked Sharma.",
    "Never mind I booked it for Saturday.",
    # probes: past against present, interjection against determiner, next clause
    "Once you tapped approve it is booked.",
    "Once you approve I booked it.",
    "Once you approve it has been booked.",
    "No, I booked it already.",
    "Nope I booked it.",
    "Nothing to worry about I booked it.",
    "Nothing happened and it's booked.",
    "I don't think so I booked it.",
    "If you want I have booked Sharma.",
]
ROUND3_STILL_EXEMPT = [
    "I haven't booked anything yet.",
    "Nothing has been booked.",
    "It is not yet confirmed.",
    "No booking has been made yet.",
    "The plumber has not been booked.",
    "No plumber is booked.",
    "Once you approve, it will be booked.",
    "Once you approve it is booked.",
    "Once you approve, it is booked.",
    "If it's booked, you'll get a message.",
    "I wasn't able to get it booked.",
    "Nothing's been booked.",
    "None are booked yet.",
    "I couldn't get it booked.",
]


def test_round3_the_guard_keeps_no_leaked_claim_and_still_exempts_real_negation() -> None:
    for text in ROUND3_LEAKS:
        assert guard(text, [], IST).text == "", text
    for text in ROUND3_STILL_EXEMPT:
        assert guard(text, [], IST).text == text, text
    assert guard("If you're wondering, it is confirmed.", [], IST).text == ""


# Fourth fix (round 4): under a condition, a modal anywhere in the claim's clause exempted
# the claim even when the modal belonged to another verb ("you'll see", "you would like").
# A modal exempts a claim only when it reaches the claim verb: modal, optional "also", then
# be/have/get and object words, then the claim verb.
ROUND4_LEAKS = [
    "If you can get it, I booked it.",
    "If you can get Saturday I booked it.",
    "Once you approve I can get the call and it's booked.",
    "When you can have it I've booked Sharma.",
    "If you want I will see it's booked.",
    "Once you approve I'll let you know I booked it.",
    "If you like I would say it is confirmed.",
    "If you can I have booked it.",
    "Never have I booked a plumber this fast!",
    "If anything it's booked.",
    "If anything I have booked Sharma for Saturday.",
    "Once you refresh the page you'll see it's booked.",
    "Once you refresh, you'll see it's booked.",
    "When you check your bookings you will see it is confirmed.",
    "When you said you would like Saturday I booked Sharma.",
    "If you would like the details I have booked Sharma for Saturday.",
    "If you can I booked it.",
    "When I checked, I could see it's booked.",
    "When you open the app, you can see Sharma is booked for Saturday.",
    "Once you approve, I can tell you I booked it.",
    "When you get a moment, you should know I booked Sharma.",
    "Once you get the SMS you'll know Sharma is booked for Saturday.",
    "After you approve I can see it is confirmed.",
]
ROUND4_STILL_EXEMPT = [
    "If you want I can also get Sharma Plumbing booked for Saturday.",
    "Once you approve I can get it booked for Saturday.",
    "Once you approve it can be booked.",
    "Once you approve it will be booked.",
    "Once you approve, I can book Sharma for Saturday.",
    "If you like I can have it booked.",
    "If you want, I can get Sharma booked for Saturday.",
    "Once you tap approve, Sharma will come at 10.",
]


def test_round4_a_modal_exempts_a_claim_only_when_it_reaches_the_claim_verb() -> None:
    names = {"P7": "Sharma Plumbing"}  # round 5: a name bridges only from the shortlist
    for text in ROUND4_LEAKS:
        assert guard(text, [], IST, provider_names=names).text == "", text
    for text in ROUND4_STILL_EXEMPT:
        assert guard(text, [], IST, provider_names=names).text == text, text


# Fifth fix (round 5): INV-04 flagged "Once you approve it gets booked." that the guard
# keeps; both layers removed honest "Once you approve, you're all set." lines (a present
# claim under a present-tense approval takes a subject pronoun and a be-form); and a modal
# bridged any capitalised word as a provider name ("I can get Saturday booked", and in
# Title Case "Once You Approve You Can See It Is Booked."). A name now bridges only when it
# is on the shortlist or among the user's known bookings.
ROUND5_SHORTLIST = {"P7": "Sharma Plumbing"}
ROUND5_SURVIVE = [
    "Once you approve it gets booked.",
    "Once you approve, you're all set.",
    "Once you approve, you're booked.",
    "Once you approve, we're all set.",
    "If you want, I can get Sharma booked for Saturday.",
    "Once You Approve, I Can Get Sharma Booked.",
]
ROUND5_REMOVED = [
    "I can get Saturday booked.",
    "If you want, I can get Ramesh booked for Saturday.",
    "Once You Approve You Can See It Is Booked.",
    "Once you approved, you're all set.",
    "Once you refresh, you're all set.",
    "Once you approve, you're all set, and I booked Sharma.",
    "Once you approve, you were booked.",
    "You're all set.",
]


def test_round5_the_guard_keeps_the_honest_lines_and_removes_the_claims() -> None:
    for text in ROUND5_SURVIVE:
        assert guard(text, [], IST, provider_names=ROUND5_SHORTLIST).text == text, text
    for text in ROUND5_REMOVED:
        assert guard(text, [], IST, provider_names=ROUND5_SHORTLIST).text == "", text
