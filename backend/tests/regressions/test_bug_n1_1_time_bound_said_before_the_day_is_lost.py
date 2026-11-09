"""BUG-N1-1: "not before 10 a.m." said before the day was known was lost.

Example 3 as the brief words it: "Book the same electrician as last time, but not before
10 a.m.", the agent asks for the day, the user answers "tomorrow". The bound is in no
window yet (a window needs a day phrase), and the answer does not repeat it, so the card
proposed 08:00. The committed S08 hid it because its scripted user restated the bound with
the day. S08 now answers with just "tomorrow" and asserts no slot before 10:00.

Ruling (9.9): a bound's span may cite an earlier user message of the same errand; the day
phrase still comes from the current one. The rule's negative cases are in
tests/agent/test_window_carry_over.py.
"""

from __future__ import annotations

from pathlib import Path

from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario
from tests.agent.test_window_carry_over import (  # noqa: F401
    test_a_bound_in_no_message_is_still_rejected,
    test_a_bound_may_cite_an_earlier_message_of_the_errand,
    test_a_quoted_bound_must_still_say_the_time_it_sets,
    test_earlier_messages_are_this_errands_only,
    test_only_a_window_bound_reads_earlier_messages,
    test_the_day_phrase_cannot_come_from_an_earlier_message,
)


async def test_the_bound_survives_the_users_one_word_answer(tmp_path: Path) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S08_same_electrician_as_last_time.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures
