"""Every scenario in scenarios/*.yaml, through the real API, with its invariants.

The catalogue is checked at collection: no scenarios, or a file that would not be
picked up (a .yml, a subdirectory), is a collection error, so `make evals` fails
instead of passing on one skipped test (BUG-M3-1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import report
from evals.dsl import find_scenarios, load_scenario
from evals.runner import run_scenario

SCENARIOS = find_scenarios(Path(__file__).parent / "scenarios")


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
async def test_scenario(path: Path, tmp_path: Path) -> None:
    scenario = load_scenario(path)
    result = await run_scenario(scenario, tmp_path)
    report.record(result, scenario.seed)
    assert result.passed, "\n".join(result.failures)
