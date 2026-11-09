"""The backstop always outlasts the longest deadline plus the critical grace (15.1)."""

import pytest
from pydantic import ValidationError

from errand.config import LimitSettings


def test_the_defaults_satisfy_the_rule() -> None:
    s = LimitSettings()
    assert s.run_backstop_s >= s.live_run_deadline_s + s.critical_grace_s + 10


def test_a_backstop_under_the_rule_is_refused() -> None:
    with pytest.raises(ValidationError, match="run_backstop_s"):
        LimitSettings(run_backstop_s=60.0)
