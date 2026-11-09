"""The eval table's trace path names the directory the row's traces were saved to."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evals.dsl import ReplaySpec
from evals.replay import ReplayRun, find_by_name
from evals.runner import _notes, trace_name


def test_a_replay_row_names_its_own_dot_replay_trace_directory() -> None:
    scenario = find_by_name("S01")
    replay = ReplayRun(spec=ReplaySpec(cassette="S01"))
    records: Any = SimpleNamespace(all_records=lambda: [], notes={})
    assert trace_name(scenario, replay) == f"{scenario.id}.replay"
    assert f"trace=backend/evals/out/{scenario.id}.replay/" in _notes(scenario, records, replay)
    # A scripted row of a booking scenario names its dedupe-mode folder (M5).
    assert _notes(scenario, records).endswith(f"trace=backend/evals/out/{scenario.id}.dedupe-on/")
