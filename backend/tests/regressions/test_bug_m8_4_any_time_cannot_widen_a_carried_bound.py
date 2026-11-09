"""BUG-M8-4: "any time this week" after "not before 10 a.m." kept the 10:00 bound.

The end-to-end proof is the two-turn API test; the rule is in test_window_carry_over.py.
"""

from __future__ import annotations

from tests.agent.test_window_carry_over import (  # noqa: F401
    test_any_time_drops_the_bounds_kept_from_earlier,
)
from tests.api.test_conversation_quality import (  # noqa: F401
    test_any_time_widens_a_time_bound_the_user_gave_earlier,
)
