"""The memory API end to end: create, forget through DELETE, then read the database and
a trace the way a user-facing path does."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from errand.memory.service import MemoryService
from errand.trace.masked import masker_for
from errand.trace.reader import read_trace, read_trace_masked
from evals.runner import harness
from tests.api.support import world

AREA_MESSAGE = (
    "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
)


def service_of(h: Any) -> MemoryService:
    service: MemoryService = h.app.state.memory
    return service


async def add(client: httpx.AsyncClient, key: str, scope: str, value: Any, cid: str) -> Any:
    response = await client.post(
        "/api/memory", json={"key": key, "scope": scope, "value": value, "client_action_id": cid}
    )
    assert response.status_code == 201, response.text
    return response.json()["memory"]


async def test_the_lifecycle_through_http(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        c = h.client
        saved = await add(c, "home_area", "global", "Indira Nagar", "a1")
        assert (saved["memory_id"], saved["status"], saved["value"]) == (
            "m-01", "active", "indiranagar",
        )  # fmt: skip
        # a retry with the same client_action_id replays the first answer
        again = await c.post(
            "/api/memory",
            json={"key": "home_area", "scope": "global", "value": "Indira Nagar",
                  "client_action_id": "a1"},
        )  # fmt: skip
        assert again.json()["memory"] == saved

        grouped = (await c.get("/api/memory")).json()
        assert [m["memory_id"] for m in grouped["preferences"]] == ["m-01"]
        assert grouped["suggestions"] == [] and grouped["history"] == []

        fixed = await c.patch(
            "/api/memory/m-01", json={"value": "koramangala", "client_action_id": "p1"}
        )
        assert fixed.status_code == 200 and fixed.json()["memory"]["supersedes"] == "m-01"
        stale = await c.patch(
            "/api/memory/m-01", json={"value": "whitefield", "client_action_id": "p2"}
        )
        assert stale.status_code == 409
        assert stale.json()["current"]["status"] == "superseded"

        detail = (await c.get("/api/memory/m-02")).json()
        assert [e["type"] for e in detail["events"]] == ["corrected"]
        assert (await c.get("/api/memory/m-99")).status_code == 404

        gone = await c.delete("/api/memory/m-02", params={"client_action_id": "d1"})
        assert gone.status_code == 200 and gone.json()["forgotten"] == ["m-02", "m-01"]
        assert gone.json()["memory"]["value"] is None
        twice = await c.delete("/api/memory/m-02", params={"client_action_id": "d2"})
        assert twice.status_code == 409 and twice.json()["current"]["status"] == "forgotten"
        replay = await c.delete("/api/memory/m-02", params={"client_action_id": "d1"})
        assert replay.status_code == 200 and replay.json() == gone.json()
        missing = await c.delete("/api/memory/m-02")
        assert missing.status_code == 422  # client_action_id is required


async def test_a_refused_add_is_422_and_stores_nothing(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        bad = await h.client.post(
            "/api/memory",
            json={"key": "home_area", "scope": "global", "value": "Atlantis",
                  "client_action_id": "x1"},
        )  # fmt: skip
        assert bad.status_code == 422 and bad.json()["reason"] == "bad_value"
        reused = await h.client.post("/api/memory/m-01/confirm", json={"client_action_id": "x1"})
        assert reused.status_code in (404, 422)
        assert (await h.client.get("/api/memory")).json() == {
            "preferences": [], "suggestions": [], "history": [],
        }  # fmt: skip


async def test_keep_dismiss_and_undo_over_http(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        for provider, scope in (("E4", "service:electrician"), ("P1", "service:plumber")):
            await service_of(h).suggest(
                user_id="u_demo", key="preferred_provider", scope=scope, value=provider,
                rule="same_provider_twice", booking_ids=["B1", "B2"],
            )  # fmt: skip
        c = h.client
        kept = await c.post("/api/memory/m-01/confirm", json={"client_action_id": "k1"})
        assert kept.json()["memory"]["source"] == "Inferred, confirmed by you"
        assert (
            await c.post("/api/memory/m-01/confirm", json={"client_action_id": "k2"})
        ).status_code == 409
        dismissed = await c.post("/api/memory/m-02/dismiss", json={"client_action_id": "d1"})
        assert dismissed.json()["memory"]["status"] == "rejected"
        saved = await add(c, "language", "global", "hindi", "a1")
        undone = await c.post(
            f"/api/memory/{saved['memory_id']}/undo", json={"client_action_id": "u1"}
        )
        assert undone.json()["memory"]["status"] == "rejected"


async def test_keep_on_a_suggestion_with_pending_siblings_succeeds_over_http(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world()) as h:
        for provider in ("E4", "E5", "E6"):
            await service_of(h).suggest(
                user_id="u_demo", key="preferred_provider", scope="service:electrician",
                value=provider, rule="same_provider_twice", booking_ids=["B1", "B2"],
            )  # fmt: skip
        c = h.client
        listed = (await c.get("/api/memory")).json()
        assert [s["memory_id"] for s in listed["suggestions"]] == ["m-01", "m-02", "m-03"]
        kept = await c.post("/api/memory/m-02/confirm", json={"client_action_id": "k1"})
        assert kept.status_code == 200 and kept.json()["memory"]["status"] == "active"
        after = (await c.get("/api/memory")).json()
        assert after["suggestions"] == []
        assert [p["memory_id"] for p in after["preferences"]] == ["m-02"]
        siblings = {r["memory_id"]: r for r in after["history"]}
        assert siblings["m-01"]["status"] == siblings["m-03"]["status"] == "rejected"
        # the siblings are not pending any more: Keep on one is a 409, not a stuck button
        late = await c.post("/api/memory/m-01/confirm", json={"client_action_id": "k2"})
        assert late.status_code == 409 and late.json()["current"]["status"] == "rejected"


async def test_after_a_forget_the_database_holds_no_plaintext_and_the_trace_reads_masked(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world()) as h:
        await add(h.client, "home_area", "global", "indiranagar", "a1")
        view = await h.say(AREA_MESSAGE)
        run_id = view.run_id

        trace = next((tmp_path / "traces").rglob(f"{run_id}.jsonl"))
        assert b"Indiranagar" in trace.read_bytes()

        done = await h.client.delete("/api/memory/m-01", params={"client_action_id": "d1"})
        assert done.status_code == 200 and done.json()["messages_redacted"] >= 1

        async with h.app.state.manager.db.session() as repos:
            dump = "\n".join([line async for line in repos.conn.iterdump()])
        assert "ndiranagar" not in dump and "ndira nagar" not in dump.lower()

        # the user's own message was overwritten in the transcript
        transcript = (await h.client.get(f"/api/conversations/{h.conversation_id}")).json()
        texts = [m["body"].get("text", "") for m in transcript["messages"]]
        assert any("[removed at your request]" in t for t in texts)
        assert not any("ndiranagar" in t for t in texts)

    # outside agent.sqlite: the raw file keeps the value, every user-facing read masks it
    raw = read_trace(trace)
    assert "Indiranagar" in str(raw.records)
    masked = read_trace_masked(trace, masker_for(tmp_path))
    assert "ndiranagar" not in str(masked.records).lower()
    assert "[removed]" in str(masked.records)
    assert [r["seq"] for r in masked.records] == [r["seq"] for r in raw.records]
    assert [r["type"] for r in masked.records] == [r["type"] for r in raw.records]
