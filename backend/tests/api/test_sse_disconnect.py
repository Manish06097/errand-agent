"""A dropped SSE connection never cancels its run, and a reconnect replays exactly once.

This runs a real uvicorn on port 0: httpx's ASGITransport buffers a whole response
until the app returns, so it cannot drop a connection mid-stream. The scripted planner
is held at its first model call, so the drop really happens mid-run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from errand.api.app import AppConfig, create_app
from errand.config import LimitSettings, ModelSettings
from errand.ids import SequentialIds
from errand.tools.runtime import in_process
from errand.trace.reader import read_trace
from errand.world.engine import WorldHost
from tests.api.support import OPENER, Gate, gated_factory, world


@asynccontextmanager
async def serve(tmp_path: Path, gate: Gate) -> AsyncIterator[tuple[str, Any]]:
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(world("sse_disconnect"), "sim")
    app = create_app(
        AppConfig(
            data_dir=tmp_path,
            world=in_process(host),
            model_factory=gated_factory(gate),
            ids=SequentialIds(),
            model_settings=ModelSettings(model_mode="scripted"),
            limits=LimitSettings(),
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        await task
        host.close()


async def read_events(
    response: httpx.Response, *, until_seq: int | None = None
) -> list[dict[str, Any]]:
    """Events with an id, as {seq, event, type}; stops at `until_seq` or run.finished."""
    events: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line:
            key, _, value = line.partition(": ")
            current[key] = value
            continue
        if "id" in current:
            data = json.loads(current["data"])
            events.append(
                {"seq": int(current["id"]), "event": current["event"], "type": data["type"]}
            )
            if current["event"] == "run.finished":
                return events
            if until_seq is not None and int(current["id"]) >= until_seq:
                return events
        current = {}
    return events


async def test_a_dropped_stream_does_not_cancel_the_run_and_replay_resumes(tmp_path: Path) -> None:
    gate = Gate()
    async with serve(tmp_path, gate) as (base, app), httpx.AsyncClient(base_url=base) as client:
        conversation = (await client.post("/api/conversations")).json()["conversation_id"]
        posted = await client.post(
            f"/api/conversations/{conversation}/messages",
            json={"client_msg_id": "m-1", "text": OPENER},
        )
        run_id = posted.json()["run_id"]
        await asyncio.wait_for(gate.waiting.wait(), 5)

        # 1. Read the first records, then drop the connection while the run is held.
        async with client.stream("GET", f"/api/runs/{run_id}/events") as response:
            first = await read_events(response, until_seq=3)
        assert [e["seq"] for e in first] == [1, 2, 3]
        await asyncio.sleep(0.2)  # let the server notice the disconnect
        assert app.state.manager.active_run(conversation) == run_id

        # 2. Reconnect mid-run with Last-Event-ID, then let the run finish while reading.
        async with client.stream(
            "GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "3"}
        ) as response:
            reader = asyncio.create_task(read_events(response))
            await asyncio.sleep(0.2)
            gate.open()
            resumed = await asyncio.wait_for(reader, 10)
        await app.state.manager.wait_idle()

        records = read_trace(tmp_path / "traces" / conversation / f"{run_id}.jsonl").records
        last = records[-1]["seq"]
        assert records[-1]["type"] == "run.end"
        assert records[-1]["outcome"] == "ASKED_USER"  # finished normally, not cancelled
        seqs = [e["seq"] for e in resumed]
        assert seqs == list(range(4, last + 1)), seqs
        assert [e["event"] for e in resumed].count("run.finished") == 1

        # 3. After the run: replay strictly after N, once, ending with run.finished.
        async with client.stream(
            "GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "5"}
        ) as response:
            after = await read_events(response)
        assert [e["seq"] for e in after] == list(range(6, last + 1))
        assert [e["event"] for e in after].count("run.finished") == 1
        assert after[-1]["event"] == "run.finished"

        convo = (await client.get(f"/api/conversations/{conversation}")).json()
        assert convo["runs"] == [
            {
                "run_id": run_id,
                "status": "ended",
                "outcome": "ASKED_USER",
                "trigger": "user_message",
            }
        ]
