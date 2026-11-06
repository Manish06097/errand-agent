"""BUG-M3-2: a restarted API in sim mode handed out a run id that already existed.

The sim id counters were seeded from COUNT(*) of conversations and runs, but an
admission can mint a run id and then not use it: a refused lease (a stale lease left
by a crashed worker) answers 409 after the id was minted. The next run then skips a
number, the table holds fewer rows than the highest id, and after a restart the
counter lands on an id that is already taken. The POST then fails on
`messages.message_id` with an IntegrityError, a 500 on the default `make dev` path.

The test does what the lead did through the API: a worker dies holding the thread's
lease, the next message is refused, the retry runs once the lease has lapsed, the API
restarts, and the next message must be admitted with a fresh run id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI

from errand.api.app import AppConfig, create_app
from errand.api.locks import LEASE_TTL_MS, thread_key
from errand.api.runs import RunManager
from errand.clock import to_ms, wall_now
from errand.config import LimitSettings, ModelSettings
from errand.store.agent_db import AgentDB
from errand.tools.runtime import in_process
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig


@dataclass
class Api:
    app: FastAPI
    client: httpx.AsyncClient

    @property
    def manager(self) -> RunManager:
        manager: RunManager = self.app.state.manager
        return manager

    async def post(self, conversation: str, text: str, msg_id: str) -> httpx.Response:
        return await self.client.post(
            f"/api/conversations/{conversation}/messages",
            json={"client_msg_id": msg_id, "text": text},
        )

    async def finish(self, run_id: str) -> None:
        await self.client.get(f"/api/runs/{run_id}/events")  # returns once the run ended
        await self.manager.wait_idle()  # and its lease is released


@asynccontextmanager
async def api(root: Path, host: WorldHost) -> AsyncIterator[Api]:
    # The `make dev` defaults: scripted mode with the reference policy, and no ids
    # passed in, so the app picks sim ids seeded from agent.sqlite itself.
    app = create_app(
        AppConfig(
            data_dir=root,
            world=in_process(host),
            model_settings=ModelSettings(model_mode="scripted"),
            limits=LimitSettings(),
        )
    )
    # A server answers an unhandled exception with a 500; so does this client.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        yield Api(app, client)


async def test_a_refused_lease_then_a_restart_does_not_reuse_a_run_id(tmp_path: Path) -> None:
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(ScenarioConfig(id="bug_m3_2", seed=7), "sim")
    try:
        async with api(tmp_path, host) as a:
            created = await a.client.post("/api/conversations")
            conversation = created.json()["conversation_id"]
            first = await a.post(conversation, "need a plumber", "m-1")
            assert first.status_code == 202
            await a.finish(first.json()["run_id"])

            # A worker on another process takes the thread and dies without releasing.
            key = thread_key(conversation)
            async with a.manager.db.session() as repos:
                stale = await repos.leases.acquire(
                    key, "api-99999:dead", to_ms(wall_now()), LEASE_TTL_MS
                )
            assert stale is not None

            refused = await a.post(conversation, "indiranagar", "m-2")
            assert refused.status_code == 409
            assert refused.json() == {"error": "run_in_progress", "run_id": None}

            # The dead worker's lease lapses; the client retries its message.
            async with a.manager.db.session() as repos:
                await repos.conn.execute(
                    "UPDATE leases SET expires_at = ? WHERE key = ?",
                    (to_ms(wall_now()) - 1, key),
                )
            retried = await a.post(conversation, "indiranagar", "m-2")
            assert retried.status_code == 202
            await a.finish(retried.json()["run_id"])

        async with api(tmp_path, host) as b:
            third = await b.post(conversation, "this week mornings, 1500", "m-3")
            assert third.status_code == 202, third.text
            run_id = third.json()["run_id"]
            await b.finish(run_id)
            convo = (await b.client.get(f"/api/conversations/{conversation}")).json()
    finally:
        host.close()

    run_ids = [r["run_id"] for r in convo["runs"]]
    assert len(run_ids) == len(set(run_ids)) == 3
    assert run_id not in (first.json()["run_id"], retried.json()["run_id"])
    assert [r["outcome"] for r in convo["runs"]] == [
        "ASKED_USER",
        "ASKED_USER",
        "PRESENTED_OPTIONS",
    ]


async def test_sim_ids_restart_past_the_highest_id_not_the_row_count(tmp_path: Path) -> None:
    # Any path that mints and then does not insert (a refused lease, a client_msg_id
    # race lost to another process) leaves a gap; the next process must start past it.
    db = await AgentDB.open(tmp_path / "agent.sqlite")
    try:
        async with db.transaction() as repos:
            for conversation_id in ("c_0001", "c_0004"):
                await repos.conn.execute(
                    "INSERT INTO users (user_id, tz, display_name) VALUES ('u_demo', 'UTC', 'D') "
                    "ON CONFLICT DO NOTHING"
                )
                await repos.conversations.create(
                    conversation_id=conversation_id, user_id="u_demo", now_ms=1
                )
            for run_id in ("r_0002", "r_0007"):
                await repos.runs.start(
                    run_id=run_id,
                    conversation_id="c_0001",
                    trigger="user_message",
                    now_ms=1,
                    trace_path=f"traces/{run_id}.jsonl",
                )
    finally:
        await db.close()

    host = WorldHost(tmp_path / "world.sqlite")
    host.load(ScenarioConfig(id="bug_m3_2_gaps", seed=7), "sim")
    try:
        async with api(tmp_path, host) as a:
            assert a.manager.ids.conversation_id() == "c_0005"
            assert a.manager.ids.run_id() == "r_0008"
    finally:
        host.close()
