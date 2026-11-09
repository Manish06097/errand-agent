"""Replay and record cases for the eval harness (architecture 18.2, 18.6).

A scenario with `replay: {cassette: S01}` gets a second case: the same steps and the
same expectations, but the planner and the quarantined reader are served from
`evals/cassettes/S01.jsonl` instead of the scripted policies. Its eval-table row is
`<id>[replay]` with model_mode `replay:S01`, and it runs offline under the socket guard
with no API key.

`make record SCENARIO=S01` runs the same case in record mode: the real key, a fresh
cassette, the simulated clock. Every request is canonical and keyed (18.2), so record
and replay hash the same.

A cassette miss must be loud. It surfaces as an INTERNAL_ERROR run whose `error` record
carries the diff against the nearest recording, and `extra_failures` puts that diff into
the row's failures, so a replay row cannot pass with a request the cassette never saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from errand.config import ModelSettings
from evals import invariants
from evals.dsl import ReplaySpec, Scenario, find_scenarios, load_scenario

EVALS = Path(__file__).resolve().parent
CASSETTES = EVALS / "cassettes"
SCENARIOS = EVALS / "scenarios"

Mode = Literal["replay", "record"]


def cassette_path(name: str) -> Path:
    return CASSETTES / f"{name}.jsonl"


@dataclass(frozen=True)
class Case:
    """One row of the eval table: a scenario, scripted or on a cassette."""

    scenario: Scenario
    replay: ReplaySpec | None = None

    @property
    def id(self) -> str:
        return f"{self.scenario.id}[replay]" if self.replay else self.scenario.id


def cases_of(scenario: Scenario) -> list[Case]:
    found = [Case(scenario)]
    if scenario.replay is not None:
        found.append(Case(scenario, scenario.replay))
    return found


@dataclass(frozen=True)
class ReplayRun:
    """What `run_scenario` needs to run a scenario on a cassette (or to record one)."""

    spec: ReplaySpec
    mode: Mode = "replay"
    # Where the cassette lives; tests point it at a temp directory.
    directory: Path = CASSETTES

    @property
    def case_id(self) -> str:
        return self.spec.cassette

    def scenario_id(self, scenario: Scenario) -> str:
        return f"{scenario.id}[replay]"

    @property
    def model_mode(self) -> str:
        return f"{self.mode}:{self.spec.cassette}"

    @property
    def path(self) -> Path:
        return self.directory / f"{self.spec.cassette}.jsonl"

    def harness_kwargs(self) -> dict[str, Any]:
        """Settings for the harness: the mode and the cassette, no scripted transports
        (the gateway and the reader build their own replay or record transports)."""
        return {
            "model_settings": ModelSettings(model_mode=self.mode, cassette_path=self.path),
            "model_factory": lambda: None,
            "reader_factory": lambda: None,
        }

    def extra_failures(self, records: invariants.Records) -> list[str]:
        """Cassette misses, with the diff the recorder prints, so they fail the row."""
        found = []
        for record in records.all_records():
            if record["type"] == "error" and record.get("exception_type") == "CassetteMiss":
                found.append(f"{record['run_id']}: {record.get('message')}")
        return found

    def notes(self, records: invariants.Records) -> str:
        """Max actual input tokens across the run's model calls (18.7)."""
        most = 0
        for record in records.all_records():
            if record["type"] == "model.call":
                u = record["usage"]
                most = max(
                    most,
                    int(u["input_uncached"])
                    + int(u["cache_read"])
                    + int(u["cache_write_5m"])
                    + int(u["cache_write_1h"]),
                )
        cassette = f"backend/evals/cassettes/{self.spec.cassette}.jsonl"
        return f"max_actual_input_tokens={most}; cassette={cassette}; "


def all_cases(directory: Path = SCENARIOS) -> list[Case]:
    return [case for path in find_scenarios(directory) for case in cases_of(load_scenario(path))]


def find_by_name(name: str, directory: Path = SCENARIOS) -> Scenario:
    """`S01` or the full id: the scenario whose id is, or starts with, `name_`."""
    matches = [
        load_scenario(p)
        for p in find_scenarios(directory)
        if p.stem == name or p.stem.startswith(f"{name}_")
    ]
    if len(matches) != 1:
        raise LookupError(f"{name!r} matches {[m.id for m in matches]} in {directory}")
    return matches[0]
