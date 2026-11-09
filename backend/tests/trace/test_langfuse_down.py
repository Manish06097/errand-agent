"""Keys set, Langfuse server down: the agent does not notice, and health says so.

The real sink and the real OTLP exporter point at a closed loopback port (the socket
guard allows loopback, so nothing here can leave the machine). Runs must complete, the
JSONL must match a run with Langfuse off, no sink call may block the run, the lifespan
exit must not wait for Langfuse longer than its timeout, and the failed exports must
show in /api/health. A per-request or per-run flush() would block for the whole timeout
(spike F8) and fail the timing checks here.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from errand.config import TraceSettings
from errand.trace.sinks import make_sink
from errand.trace.sinks.langfuse import LangfuseSink
from evals.policies import reference
from evals.runner import Harness
from tests.trace.langfuse_support import (
    TimedSink,
    Timings,
    api,
    expected_outcomes,
    fresh_keys,
    jsonl_runs,
    patched_sink,
    s03,
    s03_world,
    seed_phone,
    stable,
)

TIMEOUT_S = 2
# What this guards is that no sink call blocks the run for anything like the export
# timeout: a per-request flush() would hold a call for the whole TIMEOUT_S (spike F8).
# 250 ms is an order of magnitude under that, so a blocking call still fails here, but a
# scheduler pause on a loaded machine (one call took 70 ms with several suites running at
# once) does not make the suite fail at random.
CALL_BUDGET_MS = 250.0


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_langfuse_down_changes_nothing_but_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = fresh_keys(host=f"http://127.0.0.1:{closed_port()}", timeout=TIMEOUT_S)
    timings = Timings()
    sinks: list[TimedSink] = []

    def down() -> Any:
        sink = TimedSink(make_sink(TraceSettings(langfuse="auto"), keys), timings)
        sinks.append(sink)
        return sink

    async with api(tmp_path / "off", monkeypatch) as h:
        await s03(h)

    # The harness is entered and left by hand so the lifespan exit can be timed.
    with patched_sink(monkeypatch, down):
        h = Harness(tmp_path / "on", s03_world(), reference.policy)
        await h.__aenter__()
    try:
        seed_phone(tmp_path / "on")
        assert await s03(h) == expected_outcomes()
        assert isinstance(sinks[0].inner, LangfuseSink)
        # Export runs on the SDK's thread: wait for its first failed batch.
        deadline = time.monotonic() + 3 * TIMEOUT_S + 2
        health: dict[str, Any] = {}
        while time.monotonic() < deadline:
            health = (await h.client.get("/api/health")).json()
            if health["langfuse"]["export_failures"] > 0:
                break
            await asyncio.sleep(0.2)
        assert health["langfuse"]["state"] == "on"
        assert health["langfuse"]["host"] == keys.host
        assert health["langfuse"]["export_failures"] > 0
        # More runs while exports are failing in the background.
        assert (await h.say("and the second one?")).outcome == "REPLIED"
    finally:
        started = time.perf_counter()
        await h.__aexit__(None, None, None)
        shutdown_s = time.perf_counter() - started

    assert shutdown_s <= TIMEOUT_S + 1, f"lifespan exit took {shutdown_s:.2f}s"

    on = jsonl_runs(tmp_path / "on")
    off = jsonl_runs(tmp_path / "off")
    assert list(on)[: len(off)] == list(off)
    assert stable({run: on[run] for run in off}) == stable(off)

    assert {name for name, _ in timings.calls} >= {"write", "begin", "bind", "close"}
    name, worst = timings.worst("write", "begin", "bind", "abandon", "close")
    assert worst <= CALL_BUDGET_MS, f"{name} took {worst:.2f} ms"
