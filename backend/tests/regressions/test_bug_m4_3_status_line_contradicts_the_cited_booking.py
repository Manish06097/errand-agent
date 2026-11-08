"""BUG-M4-3: "That's already booked: B-5715 ..." sat above "Nothing booked for this
request yet."

After a verified booking the errand is COMPLETED, so a second "book it" starts a fresh
errand. Its reply cites the booking from the record, while the code status line for a
fresh errand said nothing was booked. The user saw two statements that contradict each
other. Ruling (8.7): a reply that cites a verified booking by id, on an errand that has
taken no action, gets "Booked: B-n, confirmed with the provider."

Runs S29 (a typed "book it" grants, then two more "book it" are answered from the record)
with its status line assertions.
"""

from __future__ import annotations

from pathlib import Path

from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario


async def test_the_reply_and_its_status_line_agree(tmp_path: Path) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S29_book_it_twice.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures


async def test_another_services_booking_never_becomes_this_errands_status_line(
    tmp_path: Path,
) -> None:
    """Review round 2: a plumber booking cited on an electrician errand must not turn that
    errand's own status line into "Booked: B-n"."""
    scenario = load_scenario(EVALS / "scenarios" / "S29b_book_it_for_another_service.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures
