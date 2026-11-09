"""The planner timeout counts real seconds, so a simulated deadline caps it only when the
model call takes no real time (scripted, replay). A recording or live call on a SimClock is
left on its own timeout (architecture 15.3, N3 review round 1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from errand.clock import IST, SimClock
from errand.config import ModelMode
from errand.domain.counters import RunCounters
from errand.llm.scripted import ScriptedChatModel
from tests.llm.support import rig

START = datetime(2026, 11, 9, 10, 0, tzinfo=IST)


@pytest.mark.parametrize(
    ("mode", "capped"),
    [("scripted", True), ("replay", True), ("record", False), ("live", False)],
)
def test_a_simulated_deadline_caps_only_calls_that_take_no_real_time(
    tmp_path: Path, mode: ModelMode, capped: bool
) -> None:
    counters = RunCounters(deadline_at=START + timedelta(seconds=2))
    r = rig(
        tmp_path,
        transport=ScriptedChatModel(script=[]),
        clock=SimClock(START),
        counters=counters,
    )
    # Only the mode matters to the timeout; building a live gateway needs a key.
    r.gateway.settings = r.gateway.settings.model_copy(update={"model_mode": mode})
    timeout, by_deadline = r.gateway._call_timeout()
    if capped:
        assert (timeout, by_deadline) == (2.0, True)
    else:
        assert timeout == r.gateway.limits.planner_timeout_s and by_deadline is False
