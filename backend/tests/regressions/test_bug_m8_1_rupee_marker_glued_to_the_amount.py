"""BUG-M8-1: "under Rs.1500" was rejected and the user was asked everything again.

`evidence._AMOUNT` refused a number touching a letter or a dot, so "rs.1500", "rs1500",
"inr1500", "1500rs" and "1,500rs" parsed to nothing (or to 1). The end-to-end repro is in
tests/api/test_first_minute.py; the parser rows are in tests/agent/test_evidence_parsers.py.
"""

from __future__ import annotations

from errand.agent import evidence
from tests.api.test_first_minute import (  # noqa: F401
    test_a_rupee_budget_in_any_common_spelling_fills_the_brief_and_searches,
)


def test_a_glued_rupee_marker_is_not_part_of_the_number() -> None:
    for span in ("rs.1500", "rs1500", "inr1500", "1500rs", "1,500rs", "₹1500"):
        assert evidence.parse_amounts(span) == {1500}, span
