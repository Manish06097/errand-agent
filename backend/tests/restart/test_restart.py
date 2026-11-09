"""A real kill and a real restart, as tests (the eval scenarios S36, S37 and S52 are the
same flows with the full invariants; these add the facts a scenario row cannot).

Each test starts the world and the API as processes, kills the API at a fault point,
restarts it on the same data directory and reads the records the way a user and an
auditor would. They are `slow` (second step of make check) and take their API port from
the worker's port block.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evals.dsl import load_scenario
from evals.runner import API_PORT_ENV, EVALS, run_scenario
from tests.conftest import worker_port

pytestmark = pytest.mark.slow


async def run(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv(API_PORT_ENV, str(worker_port(0)))
    scenario = load_scenario(EVALS / "scenarios" / f"{name}.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, "\n".join(result.failures)
    return result


def rows(root: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(root / "agent.sqlite")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def test_restart_after_the_world_booked_ends_the_dead_run_once_and_confirms_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run("S37_crash_after_world_book", tmp_path, monkeypatch)
    runs = rows(tmp_path, "SELECT run_id, trigger, status, outcome FROM runs ORDER BY run_id")
    assert [(r[1], r[2], r[3]) for r in runs[:3]] == [
        ("user_message", "ended", "AWAITING_APPROVAL"),
        ("approval_decision_resume", "recovered", "RUN_INTERRUPTED"),
        ("recovery", "ended", "BOOKED_VERIFIED"),
    ]
    dead = tmp_path / "traces" / "c_0001" / f"{runs[1][0]}.jsonl"
    records = [json.loads(line) for line in dead.read_text().splitlines()]
    ends = [r for r in records if r["type"] == "run.end"]
    assert len(ends) == 1 and records[-1] is ends[0]
    assert ends[0]["outcome"] == "RUN_INTERRUPTED" and ends[0]["recovered_by"] == runs[2][0]
    intents = rows(tmp_path, "SELECT state FROM action_intents")
    assert intents == [("succeeded",)]


async def test_restart_after_a_grant_nobody_acted_on_never_resumes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run("S52_crash_after_grant_commit", tmp_path, monkeypatch)
    approvals = rows(tmp_path, "SELECT status, invalidated_reason FROM approvals ORDER BY rowid")
    assert approvals[0] == ("expired", "grant_not_executed")
    # the stale grant's resume row was closed, not left for a later click
    assert (
        rows(tmp_path, "SELECT approval_id FROM pending_resumes WHERE approval_id = 'ap_r_0001'")
        == []
    )
