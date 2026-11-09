"""Every scenario in scenarios/*.yaml, through the real API, with its invariants.

The catalogue is checked at collection: no scenarios, or a file that would not be
picked up (a .yml, a subdirectory), is a collection error, so `make evals` fails
instead of passing on one skipped test (BUG-M3-1).

A scenario with `replay: {cassette: ...}` yields a second case, `<id>[replay]`, on the
same steps and expectations with the planner and reader served from the cassette (M7).
A scenario that can reach `book` runs once per world dedupe mode (M5): the id and the
table row carry `[dedupe=on]` or `[dedupe=off]`, and the harness sets the world's
`enforce_idempotency` and the agent's `booking.provider_dedupes_keys` together.
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
    result = await run_scenario(case.scenario, tmp_path, replay=replay, mode=case.mode)
    report.record(result, case.scenario.seed)
    assert result.passed, "\n".join(result.failures)
