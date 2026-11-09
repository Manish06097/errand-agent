"""Every scenario in scenarios/*.yaml, through the real API, with its invariants.

The catalogue is checked at collection: no scenarios, or a file that would not be
picked up (a .yml, a subdirectory), is a collection error, so `make evals` fails
instead of passing on one skipped test (BUG-M3-1).

A scenario with `replay: {cassette: ...}` yields a second case, `<id>[replay]`, on the
same steps and expectations with the planner and reader served from the cassette (M7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import report
from evals.replay import Case, ReplayRun
from evals.replay import all_cases as collect
from evals.runner import run_scenario

CASES = collect(Path(__file__).parent / "scenarios")


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_scenario(case: Case, tmp_path: Path) -> None:
    replay = ReplayRun(case.replay) if case.replay else None
    result = await run_scenario(case.scenario, tmp_path, replay=replay)
    report.record(result, case.scenario.seed)
    assert result.passed, "\n".join(result.failures)
