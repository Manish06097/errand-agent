"""BUG-N4-1: a user message was stamped after the assistant card it produced.

On `make dev` the world is a separate process on a `system_offset` clock, and the API
reads time from a `TrackingClock`: real time plus the offset of the last response from
the world. When something moves the world's clock backwards without going through the
API (a scenario reload straight on the world, `curl :WORLD/dev/scenario`), the API's
reading is stale until the run's first world call. The user's message is stamped at
admission with the stale reading, the card minutes later with the fresh one, and
GET /api/conversations lists the opener below its own answer.

This runs the real world process with the clock configuration `make dev` uses and the
real API app, and reads the order the way a client does.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from errand.api.app import AppConfig, create_app, scripted_factory
from errand.clock import IST
from errand.config import ApprovalSettings, BookingSettings, FollowupSettings, ModelSettings
from errand.ids import SequentialIds
from errand.tools.runtime import over_http
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals.policies import reader as reader_doubles
from evals.policies import reference
from tests.conftest import worker_port

pytestmark = pytest.mark.slow

BACKEND = Path(__file__).resolve().parents[2]
OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def scenario() -> ScenarioConfig:
    return ScenarioConfig(
        id="bug_n4_1", seed=41, world=WorldConfig(book=BookConfig(forced=["success"]))
    )


@asynccontextmanager
async def world_process(tmp_path: Path, port: int) -> AsyncIterator[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env["ERRAND_DEV"] = "1"
    env["PYTHONPATH"] = str(BACKEND)
    log = (tmp_path / "world.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "errand.world.server",
            "--port",
            str(port),
            "--db",
            str(tmp_path / "world.sqlite"),
            "--clock",
            "system_offset",
        ],
        cwd=BACKEND,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient() as http:
            while time.monotonic() < deadline:
                with contextlib.suppress(httpx.TransportError):
                    if (await http.get(f"{url}/health")).status_code == 200:
                        break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("the world did not come up")
        yield url
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        proc.kill()
        log.close()


async def load_world(url: str, config: ScenarioConfig) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"{url}/dev/scenario",
            json={"scenario": config.model_dump(mode="json"), "clock_mode": "system_offset"},
        )
        response.raise_for_status()


async def test_a_message_is_never_older_than_the_card_it_produced(tmp_path: Path) -> None:
    async with world_process(tmp_path, worker_port(0)) as url:
        await load_world(url, scenario())
        access = await over_http(url)
        app = create_app(
            AppConfig(
                data_dir=tmp_path,
                world=access,
                model_factory=scripted_factory(reference.policy),
                reader_factory=scripted_factory(reader_doubles.keyword),
                ids=SequentialIds(),
                model_settings=ModelSettings(model_mode="scripted"),
                tz=IST,
                approval_settings=ApprovalSettings(),
                booking_settings=BookingSettings(),
                followup_settings=FollowupSettings(watch_interval_s=0),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent"
            ) as client,
        ):
            conversation_id = (await client.post("/api/conversations")).json()["conversation_id"]
            # The world's clock jumps back by a few seconds behind the API's back, the
            # way a scenario reload straight on the world does.
            await asyncio.sleep(3)
            await load_world(url, scenario())
            posted = await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"client_msg_id": "cm_001", "text": OPENER},
            )
            assert posted.status_code == 202, posted.text
            convo: dict[str, Any] = {}
            for _ in range(100):
                convo = (await client.get(f"/api/conversations/{conversation_id}")).json()
                if convo["active_run_id"] is None and any(
                    m["role"] == "assistant" for m in convo["messages"]
                ):
                    break
                await asyncio.sleep(0.1)
    user_at = [m["created_at"] for m in convo["messages"] if m["role"] == "user"]
    card_at = [m["created_at"] for m in convo["messages"] if m["role"] == "assistant"]
    assert len(user_at) == 1 and card_at, convo["messages"]
    assert user_at[0] <= min(card_at), (
        f"the opener ({user_at[0]}) is newer than its card ({card_at})"
    )
    assert next(m["role"] for m in convo["messages"]) == "user"
