"""BUG-M8-2: "Actually Thursday or Friday" after "mornings only" dropped the mornings.

The new window replaced the old one whole, so S05 booked 12:00-13:00 for someone who said
mornings only, and its YAML pinned 10:00-23:59. Code now fills the sub-fields the new
expression leaves unset from the old one. The end-to-end proof is S04 and S05 (slot_hours);
the rule is in tests/agent/test_window_carry_over.py.
"""

from __future__ import annotations

from tests.agent.test_window_carry_over import (  # noqa: F401
    test_a_days_only_correction_keeps_the_mornings,
    test_any_time_widens_and_needs_its_own_evidence,
    test_not_before_joins_the_carried_day_part,
)
