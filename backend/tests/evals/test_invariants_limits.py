"""INV-09 holds on real records and fails, clause by clause, on doctored ones."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from evals import invariants
from evals.invariants import Records
from tests.evals.test_invariants import records_of


@pytest.fixture
async def s44(tmp_path: Path) -> Records:
    return await records_of("S44_loop_forever_policy", tmp_path / "s44")


@pytest.fixture
async def s01(tmp_path: Path) -> Records:
    return await records_of("S01_happy_path_plumber", tmp_path / "s01")


def inv09(rec: Records) -> list[str]:
    return invariants.INVARIANTS["INV-09"](rec)


def edit(rec: Records, run_index: int, kind: str, **fields: Any) -> None:
    run = rec.runs[run_index]["run_id"]
    for r in rec.traces[run]:
        if r["type"] == kind:
            r.update(fields)
            return
    raise AssertionError(f"no {kind} record")


def limit(rec: Records, name: str, value: float) -> None:
    run = rec.runs[0]["run_id"]
    start = next(r for r in rec.traces[run] if r["type"] == "run.start")
    start["limits"] = {**start["limits"], name: value}


def test_inv09_holds_on_real_records(s44: Records, s01: Records) -> None:
    assert inv09(s44) == [] and inv09(s01) == []


def test_inv09_is_registered_in_the_default_set() -> None:
    assert "INV-09" in invariants.DEFAULT


def test_a_run_with_no_recorded_limits_is_a_violation(s44: Records) -> None:
    edit(s44, 0, "run.start", limits=None)
    assert "records no limits" in inv09(s44)[0]


def test_inv09_fails_when_model_calls_exceed_the_cap(s44: Records) -> None:
    limit(s44, "model_calls", 3)
    assert any("model calls, over 3" in v for v in inv09(s44))


def test_inv09_fails_when_one_plan_iterates_too_often(s44: Records) -> None:
    limit(s44, "iterations", 2)
    assert any("in one plan, over 2 iterations" in v for v in inv09(s44))


def test_inv09_fails_when_tool_calls_exceed_the_cap(s44: Records) -> None:
    limit(s44, "tool_calls", 2)
    assert any("tool calls outside the critical section, over 2" in v for v in inv09(s44))


def test_inv09_does_not_count_critical_section_tool_calls(s01: Records) -> None:
    # Cap the rest at exactly what happens outside execute and verify: no violation, so
    # the critical section's own calls (the hold re-check, the book, the status check)
    # were not counted.
    total = outside = 0
    for run in s01.runs:
        depth = 0
        for r in s01.traces[run["run_id"]]:
            if r["type"] == "node.start" and r["node"] in ("execute", "verify"):
                depth += 1
            elif r["type"] == "node.end" and r["node"] in ("execute", "verify"):
                depth -= 1
            elif r["type"] == "tool.call":
                total += 1
                outside += 0 if depth else 1
    assert outside < total
    for run in s01.runs:
        start = next(r for r in s01.traces[run["run_id"]] if r["type"] == "run.start")
        start["limits"] = {**start["limits"], "tool_calls": outside}
    assert inv09(s01) == []


def test_inv09_fails_on_a_reprompt_over_the_cap(s44: Records) -> None:
    run = s44.runs[0]["run_id"]
    base = next(r for r in s44.traces[run] if r["type"] == "model.call")
    for i in range(2):
        s44.traces[run].append({**base, "type": "guard.reprompt", "seq": 900 + i, "detail": {}})
    assert any("2 reprompts, over 1" in v for v in inv09(s44))


def test_inv09_fails_on_a_second_replan(s44: Records) -> None:
    run = s44.runs[0]["run_id"]
    starts = [r for r in s44.traces[run] if r["type"] == "node.start" and r["node"] == "plan"]
    for i in range(2):
        s44.traces[run].append({**starts[0], "seq": 800 + i})
    assert any("replans, over 1" in v for v in inv09(s44))


def test_inv09_fails_on_too_many_status_checks(s44: Records) -> None:
    run = s44.runs[0]["run_id"]
    call = next(r for r in s44.traces[run] if r["type"] == "tool.call")
    for i in range(5):
        s44.traces[run].append({**call, "tool": "get_booking_status", "seq": 700 + i})
    assert any("5 status checks, over 4" in v for v in inv09(s44))


def shift_end(rec: Records, seconds: float) -> None:
    run = rec.runs[0]["run_id"]
    end = next(r for r in rec.traces[run] if r["type"] == "run.end")
    end["ts"] = (datetime.fromisoformat(end["ts"]) + timedelta(seconds=seconds)).isoformat()


def test_inv09_fails_when_the_deadline_is_passed_with_no_intent(s44: Records) -> None:
    shift_end(s44, 61)
    assert any("with no intent open, over 60 s" in v for v in inv09(s44))


def test_inv09_grace_with_an_intent(s01: Records) -> None:
    run = s01.runs[-1]["run_id"]
    assert any(r["type"] == "intent.written" for r in s01.traces[run])
    end = next(r for r in s01.traces[run] if r["type"] == "run.end")
    start = next(r for r in s01.traces[run] if r["type"] == "run.start")
    base = (datetime.fromisoformat(end["ts"]) - datetime.fromisoformat(start["ts"])).total_seconds()
    end["ts"] = (datetime.fromisoformat(start["ts"]) + timedelta(seconds=75)).isoformat()
    assert not any(run in v and "over 80 s" in v for v in inv09(s01)), base
    end["ts"] = (datetime.fromisoformat(start["ts"]) + timedelta(seconds=81)).isoformat()
    assert any("with an intent open, over 80 s" in v for v in inv09(copy.deepcopy(s01)))
