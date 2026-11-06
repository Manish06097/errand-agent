"""A bug inside the world still leaves exactly one finished op_log row per call.

The faults are planted with monkeypatch, so these run in process and against the
ASGI app in this process rather than through the contract suite's subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from errand.tools.client import InProcessTransport
from errand.world.contracts import BookOut, GetProviderOut, HoldSlotOut
from errand.world.engine import World, WorldHost
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from errand.world.server import ServeSettings, create_app


def boom(*_: object) -> NoReturn:
    raise RuntimeError("planted bug")


@pytest.fixture
def host(tmp_path: Path) -> WorldHost:
    made = WorldHost(tmp_path / "world.sqlite")
    made.load(
        ScenarioConfig(
            id="failures", seed=2, world=WorldConfig(book=BookConfig(forced=["success"]))
        )
    )
    return made


async def test_a_plain_tool_that_raises_logs_internal_error(
    host: WorldHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = InProcessTransport(host)
    with monkeypatch.context() as patch:
        patch.setattr(World, "_get_provider", boom)
        with pytest.raises(RuntimeError, match="planted bug"):
            await client.get_provider("P3")
    assert isinstance(await client.get_provider("P3"), GetProviderOut)
    log = host.world.op_log()
    assert [(r.op, r.outcome, r.http_status) for r in log] == [
        ("get_provider", "internal_error", 500),
        ("get_provider", "ok", 200),
    ]
    assert all(r.done_seq is not None for r in log)
    host.close()


async def test_a_keyed_tool_that_raises_logs_internal_error_and_does_not_apply(
    host: WorldHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = InProcessTransport(host)
    details = await client.get_provider("P3")
    assert isinstance(details, GetProviderOut)
    held = await client.hold_slot("P3", details.available_slots[0])
    assert isinstance(held, HoldSlotOut)
    with monkeypatch.context() as patch:
        patch.setattr(World, "_book", boom)
        with pytest.raises(RuntimeError, match="planted bug"):
            await client.book(held.hold_id, "k-bug")
    op = host.world.store.get_operation("k-bug")
    assert op is not None and op.outcome == "not_applied"
    # The latch was released and nothing applied, so the same key is evaluated afresh.
    assert isinstance(await client.book(held.hold_id, "k-bug"), BookOut)
    book_rows = [r for r in host.world.op_log() if r.op == "book"]
    assert [(r.outcome, r.http_status, r.attempt) for r in book_rows] == [
        ("internal_error", 500, None),
        ("success", 200, 1),
    ]
    host.close()


def test_over_http_the_client_gets_a_500_and_the_row_is_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(ServeSettings(db=tmp_path / "world.sqlite", clock="sim"), dev=True)
    monkeypatch.setattr(World, "_get_provider", boom)
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.post("/tools/get_provider", content=json.dumps({"provider_id": "P3"}))
        assert response.status_code == 500
        [row] = [r for r in http.get("/dev/op_log").json() if r["op"] == "get_provider"]
    assert (row["outcome"], row["http_status"]) == ("internal_error", 500)
    assert row["done_seq"] is not None
