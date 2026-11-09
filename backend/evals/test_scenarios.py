"""Every scenario in scenarios/*.yaml, through the real API, with its invariants.

The catalogue is checked at collection: no scenarios, or a file that would not be
picked up (a .yml, a subdirectory), is a collection error, so `make evals` fails
instead of passing on one skipped test (BUG-M3-1).

A scenario that can reach `book` runs once per world dedupe mode (M5): the id and the
table row carry `[dedupe=on]` or `[dedupe=off]`, and the harness sets the world's
`enforce_idempotency` and the agent's `booking.provider_dedupes_keys` together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import report
from evals.dsl import Dedupe, Scenario, find_scenarios, load_scenario
from evals.runner import run_scenario

SCENARIOS = find_scenarios(Path(__file__).parent / "scenarios")
LOADED: dict[Path, Scenario] = {p: load_scenario(p) for p in SCENARIOS}
CASES = [(path, mode) for path in SCENARIOS for mode in LOADED[path].modes()]


@pytest.mark.parametrize(("path", "mode"), CASES, ids=[LOADED[p].label(m) for p, m in CASES])
async def test_scenario(path: Path, mode: Dedupe, tmp_path: Path) -> None:
    scenario = LOADED[path]
    result = await run_scenario(scenario, tmp_path, mode=mode)
    report.record(result, scenario.seed)
    assert result.passed, "\n".join(result.failures)
