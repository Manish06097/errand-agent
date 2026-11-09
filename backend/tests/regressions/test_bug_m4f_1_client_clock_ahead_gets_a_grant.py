"""BUG-M4F-1: a client clock ahead of the agent clock got a typed grant.

`typed_grant.decide` only checked lower bounds (the render ack and the request come
before `client_composed_at`), so a client whose clock ran ahead could stamp a "book it"
it typed before the card rendered with a time after the render. The test that should
have caught it stamped from the wall clock, so it passed while the wall clock was
behind the world clock and failed the day it passed it.

Every case here is stated against the agent's own clock (`X-Agent-Clock`), never the
wall clock, so it cannot flip with the date. A client synced to the agent clock is
still granted; one an hour ahead or an hour behind is an ordinary message, with 0
`book` ops.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner import harness
from tests.api.test_agent_clock import _type
from tests.api.test_approvals import ops, to_card, world

HOUR_MS = 3_600_000


@pytest.mark.parametrize("skew_ms", [HOUR_MS, -HOUR_MS], ids=["ahead_1h", "behind_1h"])
async def test_a_skewed_client_clock_is_an_ordinary_message(tmp_path: Path, skew_ms: int) -> None:
    async with harness(tmp_path, world(), clock_mode="system_offset") as h:
        await to_card(h)
        agent_now = await h.now_ms()
        composed = agent_now + skew_ms
        routed = await _type(h, composed + 1_000, composed)
        rec = await h.records()
    assert routed["routed_as"] != "grant"
    assert ops(rec.op_log, "book") == 0


async def test_a_client_synced_to_the_agent_clock_still_gets_a_grant(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), clock_mode="system_offset") as h:
        await to_card(h)
        ack = await h.tick()
        composed = await h.tick()
        routed = await _type(h, composed, ack)
        rec = await h.records()
    assert routed["routed_as"] == "grant"
    assert ops(rec.op_log, "book") == 1
