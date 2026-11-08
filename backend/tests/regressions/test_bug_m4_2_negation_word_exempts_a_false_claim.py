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
