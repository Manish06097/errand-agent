"""BUG-N3-1: a read cut by the deadline cap still took its full latency in simulated time.

With every read taking 4.5 s of world time, the call that starts with 1.5 s of run time
left has its timeout capped to 1.5 s (15.1). The in-process transport let the world
answer first, which moved the simulated clock the whole 4.5 s, and only then compared
that with the cap. The call was reported as a timeout, but the run's simulated elapsed
time ended at 63.5 s against a 60 s deadline. INV-09 found it on S45. The repro drives
the same run through the real API and applies the invariant to its records.
"""

from __future__ import annotations

from pathlib import Path

from errand.config import LimitSettings
from errand.world.scenario import ScenarioConfig, WorldConfig
from evals import invariants
from evals.policies.limits import loop_forever_x3_policy
from evals.runner import harness
from tests.api.support import OPENER


async def test_a_read_cut_by_the_deadline_does_not_run_past_it(tmp_path: Path) -> None:
    slow = ScenarioConfig(id="n3_1", seed=7, world=WorldConfig(latency_ms={"default": 4_500}))
    limits = LimitSettings(loop_iterations=12)
    async with harness(tmp_path, slow, policy=loop_forever_x3_policy, limits=limits) as h:
        view = await h.say(OPENER)
        rec = await h.records()
    assert view.outcome == "WALL_CLOCK_EXCEEDED"
    assert invariants.INVARIANTS["INV-09"](rec) == []
