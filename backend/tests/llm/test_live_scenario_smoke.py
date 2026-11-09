"""The live smoke: five replay scenarios on the real models, invariants only (architecture
18.2, ERRAND_LIVE=1, `make smoke`; never part of `make check` or `make evals`).

Each of S01, S07, S14, S22 and S39 runs through the real scenario runner with the real
Opus planner and Haiku reader (model mode `live`, the simulated clock and world). The
scripted steps are not asserted: the real model may phrase, order or choose differently, and
a step that expects the recorded path may stop the scenario early. What must hold whatever
it does is the product's own contract:

- every default invariant (INV-01 to INV-20) holds on the records of the whole scenario;
- every run ends in an outcome from the closed set, and none ends INTERNAL_ERROR or
  RUN_INTERRUPTED (a run that never ended, a crash in our code);
- the total cost of the five scenarios stays under the cap, so a runaway loop fails the run
  instead of spending (the check runs after every scenario).

The cost of each scenario is printed. With ERRAND_SMOKE_TRACE_DIR set, each scenario's traces
are copied to `<dir>/<scenario>/` afterwards, so `errand-trace stats` can measure them.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from errand.config import ModelSettings
from errand.domain.outcomes import RunOutcome
from evals import invariants
from evals.dsl import ReplaySpec, Scenario, load_scenario
from evals.replay import SCENARIOS, ReplayRun, find_by_name
from evals.runner import run_scenario

TOTAL_COST_CAP_USD = 1.50
FORBIDDEN_OUTCOMES = {RunOutcome.INTERNAL_ERROR.value, RunOutcome.RUN_INTERRUPTED.value}

# S39 has many files (one per leg); the base leg is the smoke's.
SCENARIO_FILES = {
    "S01": None,
    "S07": None,
    "S14": None,
    "S22": None,
    "S39": "S39_provider_cancels_and_user_rebooks",
}

_spent: dict[str, float] = {}


@dataclass(frozen=True)
class LiveRun(ReplayRun):
    """A `ReplayRun` for the harness whose models are the real ones and nothing is recorded."""

    @property
    def model_mode(self) -> str:
        return "live"

    def harness_kwargs(self) -> dict[str, Any]:
        return {
            "model_settings": ModelSettings(model_mode="live"),
            "model_factory": lambda: None,
            "reader_factory": lambda: None,
        }

    def extra_failures(self, records: invariants.Records) -> list[str]:
        return []

    def scenario_id(self, scenario: Scenario) -> str:
        return f"{scenario.id}[live]"


def cost_of(records: invariants.Records) -> float:
    ends = [r for r in records.all_records() if r["type"] == "run.end" and r.get("totals")]
    return sum(float(r["totals"]["cost_usd"]) for r in ends)


@pytest.mark.live
@pytest.mark.parametrize("name", list(SCENARIO_FILES))
async def test_the_scenario_holds_its_invariants_on_the_real_models(
    name: str, tmp_path: Path
) -> None:
    stem = SCENARIO_FILES[name]
    scenario = load_scenario(SCENARIOS / f"{stem}.yaml") if stem else find_by_name(name)
    captured: list[invariants.Records] = []
    result = await run_scenario(
        scenario,
        tmp_path,
        replay=LiveRun(ReplaySpec(cassette="live"), directory=tmp_path),
        on_records=captured.append,
    )
    (records,) = captured
    keep = os.environ.get("ERRAND_SMOKE_TRACE_DIR")
    if keep and (tmp_path / "traces").is_dir():
        shutil.copytree(tmp_path / "traces", Path(keep) / name, dirs_exist_ok=True)

    violated = [f for f in result.failures if f.startswith("INV-")]
    assert not violated, "\n".join(violated)

    ends = [r for r in records.all_records() if r["type"] == "run.end" and r.get("outcome")]
    assert ends, f"{name}: no run ended"
    outcomes = [str(r["outcome"]) for r in ends]
    assert all(o in {x.value for x in RunOutcome} for o in outcomes), outcomes
    assert not FORBIDDEN_OUTCOMES & set(outcomes), outcomes

    spent = cost_of(records)
    _spent[name] = spent
    print(f"\nlive smoke {name}: outcomes={outcomes} cost=${spent:.4f}")
    total = sum(_spent.values())
    print(f"live smoke running total: ${total:.4f} of ${TOTAL_COST_CAP_USD:.2f}")
    assert total < TOTAL_COST_CAP_USD, f"the smoke spent ${total:.4f}, over the cap"
