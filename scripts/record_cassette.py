"""`make record SCENARIO=S01`: record a scenario's cassette with the real model.

    python scripts/record_cassette.py S01

Runs the scenario in record mode (architecture 18.2): the real Opus planner and Haiku
reader through the API key in ERRAND_/ANTHROPIC_API_KEY or the gitignored .env, the
simulated clock, a fresh cassette (the old one is removed first, never edited by hand).
Planner and reader share one cassette, keyed by the sha256 of each canonical request, so
`make evals` replays exactly what was recorded here.

Prints the outcome, the model calls and tokens per run, and the cost. The key is never
printed. Exits 1 if the scenario's own expectations or invariants failed against the real
model (the cassette is still written, for inspection, and is not to be committed).

The scenario must carry `replay: {cassette: <name>}`.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from evals import invariants  # noqa: E402
from evals.replay import ReplayRun, find_by_name  # noqa: E402
from evals.runner import run_scenario  # noqa: E402


def summarize(records: invariants.Records) -> tuple[list[str], float]:
    lines: list[str] = []
    total = 0.0
    for run_id, trace in records.traces.items():
        calls = [r for r in trace if r["type"] == "model.call"]
        reads = [r for r in trace if r["type"] == "reader.call" and r.get("usage")]
        end = next((r for r in trace if r["type"] == "run.end"), None)
        if end is None:
            continue
        totals = end["totals"]
        total += float(totals["cost_usd"])
        latency = sum(float(r["latency_ms"]) for r in calls)
        lines.append(
            f"  {run_id}: {end['outcome']}, {len(calls)} planner calls, {len(reads)} reader calls, "
            f"tokens in/out {totals['tokens_in']}/{totals['tokens_out']}, "
            f"${float(totals['cost_usd']):.4f}, planner time {latency / 1000:.1f}s"
        )
    return lines, total


async def record(name: str) -> int:
    scenario = find_by_name(name)
    if scenario.replay is None:
        print(f"record: {scenario.id} has no `replay: {{cassette: ...}}` key", file=sys.stderr)
        return 2
    run = ReplayRun(scenario.replay, mode="record")
    run.path.unlink(missing_ok=True)
    captured: list[invariants.Records] = []
    with tempfile.TemporaryDirectory(prefix="errand-record-") as tmp:
        result = await run_scenario(scenario, Path(tmp), replay=run, on_records=captured.append)
    lines, total = summarize(captured[0])
    print(f"recorded {scenario.id} into {run.path.relative_to(REPO)}")
    print("\n".join(lines))
    print(f"cost: ${total:.4f}")
    if not result.passed:
        print(
            f"record: the scenario FAILED against the real model:\n{result.actual}", file=sys.stderr
        )
        for failure in result.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("scenario passed against the real model")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: record_cassette.py SCENARIO   (for example S01)", file=sys.stderr)
        return 2
    return asyncio.run(record(argv[0]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
