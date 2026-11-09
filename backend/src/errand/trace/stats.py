"""`errand-trace stats`: latency and cost of the planning turns in a data directory.

A planning turn is a run with at least one `model.call` (a run that only resumed an
approval or clicked a slot makes none). Its latency is `run.start` to `run.end` on the
records' own timestamps, so it includes the world's tool calls, which is what the live
run deadline must cover (architecture 15.1). The deadline is ceil(2 x p95).

    errand-trace stats [--data-dir DIR] [--mode live]

Only runs whose model calls were made in the given mode (`live` or `record` by default,
`--mode all` for everything) count, so scripted runs never feed the measurement. Traces
are read through `trace/reader.py`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.trace.reader import TraceFormatError, read_trace

REAL_MODES = ("live", "record")


@dataclass(frozen=True)
class Turn:
    run_id: str
    latency_s: float
    cost_usd: float
    model_calls: int


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the common 'inclusive' definition)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def turn_of(records: Sequence[Mapping[str, Any]], modes: Iterable[str] | None) -> Turn | None:
    calls = [r for r in records if r.get("type") == "model.call"]
    if not calls:
        return None
    if modes is not None and not any(c.get("mode") in set(modes) for c in calls):
        return None
    start = next((r for r in records if r.get("type") == "run.start"), None)
    end = next((r for r in records if r.get("type") == "run.end"), None)
    if start is None or end is None:
        return None
    latency = (
        datetime.fromisoformat(end["ts"]) - datetime.fromisoformat(start["ts"])
    ).total_seconds()
    # Simulated clocks make `ts` meaningless; wall_ts is real time.
    wall = (
        datetime.fromisoformat(end["wall_ts"]) - datetime.fromisoformat(start["wall_ts"])
    ).total_seconds()
    return Turn(
        run_id=str(start["run_id"]),
        latency_s=wall if wall > 0 else latency,
        cost_usd=float(end["totals"]["cost_usd"]),
        model_calls=int(end["totals"]["model_calls"]),
    )


def collect(data_dir: Path, modes: Iterable[str] | None) -> list[Turn]:
    turns: list[Turn] = []
    for path in sorted((data_dir / "traces").glob("*/*.jsonl")):
        try:
            records = read_trace(path).records
        except TraceFormatError:
            continue
        turn = turn_of(records, modes)
        if turn is not None:
            turns.append(turn)
    return turns


def report(turns: Sequence[Turn]) -> list[str]:
    if not turns:
        return ["no planning turns found"]
    lat = [t.latency_s for t in turns]
    cost = [t.cost_usd for t in turns]
    p95 = percentile(lat, 0.95)
    return [
        f"planning turns: {len(turns)}",
        f"latency  p50 {percentile(lat, 0.5):.2f} s  p95 {p95:.2f} s  max {max(lat):.2f} s",
        f"cost     p50 ${percentile(cost, 0.5):.4f}  p95 ${percentile(cost, 0.95):.4f}  "
        f"total ${sum(cost):.4f}",
        f"live_run_deadline_s = ceil(2 x p95) = {math.ceil(2 * p95)}",
    ]


def run(data_dir: Path, mode: str) -> int:
    modes = None if mode == "all" else REAL_MODES if mode == "live" else (mode,)
    for line in report(collect(data_dir, modes)):
        print(line)
    return 0
