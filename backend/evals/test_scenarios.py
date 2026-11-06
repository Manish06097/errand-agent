"""Every scenario in scenarios/*.yaml, through the real API, with its invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import report
from evals.dsl import load_scenario
from evals.runner import run_scenario

SCENARIOS = sorted((Path(__file__).parent / "scenarios").glob("*.yaml"))


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
async def test_scenario(path: Path, tmp_path: Path) -> None:
    scenario = load_scenario(path)
    result = await run_scenario(scenario, tmp_path)
    report.record(result, scenario.seed)
    assert result.passed, "\n".join(result.failures)
