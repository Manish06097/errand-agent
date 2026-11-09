"""The agent clock on the API (5.7, 17.3): a client that syncs to it gets a typed grant.

On `make dev` the world runs `system_offset`, so the agent's `now` is real time plus an
offset (about 14.5 hours there). A client that stamps `client_composed_at` from its own
clock composes "before the request" and is parked on a plain reminder; a client that
reads `X-Agent-Clock` (or `clock` in GET /api/conversations/{id}) is granted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from errand.clock import AGENT_CLOCK_HEADER, from_ms, parse_clock_header
from evals.runner import Harness, harness
from tests.api.test_approvals import card_of, ops, to_card, world


async def _type(h: Harness, composed_ms: int, ack_ms: int) -> dict[str, Any]:
    card = await card_of(h)
    visible = {
        "approval_id": card["approval_id"],
        "fingerprint": card["body"]["fingerprint"],
        "message_id": card["message_id"],
    }
    await h.rendered(card["message_id"], ack_ms)
    response = await h.post(
        "book it",
        h.next_client_msg_id(),
        visible_card=visible,
        client_composed_at=from_ms(composed_ms).isoformat(),
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    await h.finish(body["run_id"])
    return body


async def test_every_api_response_carries_the_agent_clock(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), clock_mode="system_offset") as h:
        health = await h.client.get("/api/health")
        convo = await h.client.get(f"/api/conversations/{h.conversation_id}")
        missing = await h.client.get("/api/conversations/c_9999")
    header = parse_clock_header(health.headers[AGENT_CLOCK_HEADER])
    assert header.mode == "system_offset"
    assert abs(header.offset_ms) > 60_000  # the world's clock is not real time
    assert missing.status_code == 404 and AGENT_CLOCK_HEADER in missing.headers
    body = convo.json()["clock"]
    assert (body["mode"], body["offset_ms"]) == ("system_offset", header.offset_ms) or (
        abs(body["offset_ms"] - header.offset_ms) < 5_000
    )
    assert abs(body["now_ms"] - header.now_ms) < 5_000


async def test_a_client_synced_to_the_agent_clock_is_granted(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), clock_mode="system_offset") as h:
        await to_card(h)
        ack = await h.tick()
        composed = await h.tick()
        routed = await _type(h, composed, ack)
        rec = await h.records()
    assert routed["routed_as"] == "grant"
    assert ops(rec.op_log, "book") == 1


async def test_a_client_on_its_own_clock_gets_an_ordinary_message(tmp_path: Path) -> None:
    """A client that never read the agent clock is ordinary. Stated against the agent's
    clock, not the wall clock, so it cannot flip with the date (BUG-M4F-1): the
    world clock starts at a fixed date, and the wall clock passes it."""
    async with harness(tmp_path, world(), clock_mode="system_offset") as h:
        await to_card(h)
        agent_now = await h.now_ms()
        routed = await _type(h, agent_now - 3_600_000 + 2_000, agent_now - 3_600_000 + 1_000)
        header = (await h.client.get("/api/health")).headers[AGENT_CLOCK_HEADER]
        rec = await h.records()
    assert routed["routed_as"] != "grant"
    assert ops(rec.op_log, "book") == 0
    assert parse_clock_header(header).offset_ms != 0
