"""`errand-trace stats` (M7): p50 and p95 latency per planning turn, and the live deadline
rule ceil(2 x p95) + reader timeout that `LimitSettings.live_run_deadline_s` is set from (15.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errand.api.app import scripted_factory
from errand.config import LimitSettings, ModelSettings
from errand.trace import stats
from errand.trace.cli import main
from evals.policies import reader as reader_doubles
from evals.policies import reference
from evals.runner import harness
from tests.api.test_approvals import OPENER, world


def test_percentiles_interpolate() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert stats.percentile(values, 0.5) == 3.0
    assert stats.percentile(values, 0.95) == pytest.approx(8.8)
    assert stats.percentile([7.0], 0.95) == 7.0


def _trace(run_id: str, seconds: float, mode: str, calls: int) -> list[dict[str, Any]]:
    start = "2026-11-09T10:00:00.000+05:30"
    end_s = f"2026-11-09T10:00:{seconds:06.3f}+05:30"
    base = {"run_id": run_id, "thread_id": "c_1"}
    records: list[dict[str, Any]] = [
        {**base, "type": "run.start", "ts": start, "wall_ts": start},
    ]
    records += [{**base, "type": "model.call", "mode": mode} for _ in range(calls)]
    records.append(
        {
            **base,
            "type": "run.end",
            "ts": end_s,
            "wall_ts": end_s,
            "totals": {"cost_usd": 0.05, "model_calls": calls},
        }
    )
    return records


def test_a_planning_turn_is_a_run_with_a_model_call_in_a_real_mode(tmp_path: Path) -> None:
    folder = tmp_path / "traces" / "c_1"
    folder.mkdir(parents=True)
    runs = {
        "r_1": (10.0, "live", 2),
        "r_2": (20.0, "live", 1),
        "r_3": (1.0, "live", 0),
        "r_4": (99.0, "scripted", 3),
    }
    for run_id, (seconds, mode, calls) in runs.items():
        lines = [json.dumps(r) for r in _trace(run_id, seconds, mode, calls)]
        (folder / f"{run_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    turns = stats.collect(tmp_path, stats.REAL_MODES)
    assert sorted((t.run_id, t.latency_s) for t in turns) == [("r_1", 10.0), ("r_2", 20.0)]
    lines = stats.report(turns)
    assert lines[0] == "planning turns: 2"
    # p95 = 19.5 -> 39, plus the reader timeout (20 s)
    assert lines[-1] == "live_run_deadline_s = ceil(2 x p95) + reader timeout 20 s = 59"


async def test_stats_over_a_scripted_run_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with harness(tmp_path, world()) as h:
        await h.say(OPENER)
        await h.manager.wait_idle()
    assert main(["stats", "--data-dir", str(tmp_path), "--mode", "all"]) == 0
    out = capsys.readouterr().out
    assert "planning turns: 1" in out and "p95" in out
    assert main(["stats", "--data-dir", str(tmp_path)]) == 0
    assert "no planning turns found" in capsys.readouterr().out  # scripted is not a real mode


async def test_the_live_deadline_applies_only_to_live_and_record_runs(tmp_path: Path) -> None:
    limits = LimitSettings(run_deadline_s=60.0, live_run_deadline_s=200.0, run_backstop_s=300.0)

    async def deadline(root: Path, mode: str) -> float:
        extra: dict[str, Any] = {}
        if mode == "record":
            extra = {
                "model_settings": ModelSettings(
                    model_mode="record", cassette_path=root / "c.jsonl"
                ),
                "model_factory": scripted_factory(reference.policy),
                "reader_factory": scripted_factory(reader_doubles.keyword),
            }
        async with harness(root, world(), limits=limits, **extra) as h:
            await h.say(OPENER)
            counters = (await h.conversation())["state"]["counters"]
        from datetime import datetime

        span = datetime.fromisoformat(counters["deadline_at"]) - datetime.fromisoformat(
            counters["started_at"]
        )
        return span.total_seconds()

    assert await deadline(tmp_path / "scripted", "scripted") == 60.0
    assert await deadline(tmp_path / "record", "record") == 200.0
