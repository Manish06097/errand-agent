"""BUG-M8-3: part_of_day was not tied to evidence ("evening" passed with the quote "this week").

The end-to-end proof is S60c (a hostile planner); the value rule is in
tests/agent/test_check_evidence.py.
"""

from __future__ import annotations

from tests.agent.test_check_evidence import (  # noqa: F401
    test_a_day_part_the_patch_sets_needs_a_day_part_word_in_the_spans,
    test_day_part_words_and_any_time,
)
