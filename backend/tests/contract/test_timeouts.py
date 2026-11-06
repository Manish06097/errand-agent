"""Timeout after success, the late commit, and the demo clock.

Over HTTP these are real network timeouts: the client gives up after its `book`
timeout from tools/timeouts.py (shrunk here through settings), while the world has
already committed, or commits later.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from errand.tools.errors import ToolTimeout
from errand.tools.timeouts import ToolTimeouts
from errand.world.contracts import BookingStatusOut, BookOut
from errand.world.scenario import (
    BookConfig,
    CommitLatency,
    FaultsConfig,
    ScenarioConfig,
    WorldConfig,
)
from tests.contract.conftest import Env
from tests.contract.helpers import book_ok, hold_first_free, rows

BOOK_TIMEOUT_S = 0.6
# The late commit lands this long after the claim: the status check has from the
# first client's timeout (0.6 s) until then to see not_found.
LATE_COMMIT_MS = 1500
# The retry gets its own client so it can outwait the commit on a slow machine.
RETRY_BOOK_TIMEOUT_S = 3.0


def forced(*outcomes: str, faults: FaultsConfig | None = None) -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "id": "timeouts",
            "seed": 9,
            "world": WorldConfig.model_validate(
                {"book": BookConfig.model_validate({"forced": list(outcomes)})}
            ),
            "faults": faults or FaultsConfig(),
        }
    )


async def test_timeout_after_success_commits_before_the_client_gives_up(env: Env) -> None:
    client = await env.load(forced("timeout_booked"), timeouts=ToolTimeouts(book=BOOK_TIMEOUT_S))
    held = await hold_first_free(client)
    world_before = await env.inspector.now()
    started = time.monotonic()
    with pytest.raises(ToolTimeout) as raised:
        await client.book(held.hold_id, "k-tab")
    waited = time.monotonic() - started
    assert raised.value.timeout_s == BOOK_TIMEOUT_S
    if env.kind == "http":
        # A real client timeout: it waited its own timeout, not the world's hold-open.
        assert BOOK_TIMEOUT_S <= waited < BOOK_TIMEOUT_S + 1.5
    # World time moved to the client's deadline on both transports.
    assert await env.inspector.now() - world_before == timedelta(seconds=BOOK_TIMEOUT_S)

    (row,) = rows(await env.inspector.op_log(), "book", "k-tab")
    assert row.outcome == "timeout_booked"
    assert row.http_status is None, "nothing reached the client"
    assert row.done_ts is not None
    deadline_ms = round((world_before + timedelta(seconds=BOOK_TIMEOUT_S)).timestamp() * 1000)
    assert row.done_ts < deadline_ms, "the commit happened before the client gave up"
    (booking,) = [b for b in await env.inspector.bookings() if b.created_by_key == "k-tab"]

    status = await client.get_booking_status(idempotency_key="k-tab")
    assert isinstance(status, BookingStatusOut)
    assert (status.status, status.booking_id) == ("confirmed", booking.booking_id)
    # A same-key retry gets the stored success body, which is what the provider committed.
    retry = await book_ok(client, held.hold_id, "k-tab")
    assert retry.booking_id == booking.booking_id
    assert rows(await env.inspector.op_log(), "book", "k-tab")[-1].outcome == "replayed"


async def test_late_commit_lands_after_the_client_timeout(env: Env) -> None:
    if env.kind == "inprocess":
        pytest.skip("a late commit needs a real wait; S53 runs against the HTTP world")
    faults = FaultsConfig(commit_latency_ms=CommitLatency(book=LATE_COMMIT_MS))
    client = await env.load(
        forced("success", "success", faults=faults), timeouts=ToolTimeouts(book=BOOK_TIMEOUT_S)
    )
    held = await hold_first_free(client)
    with pytest.raises(ToolTimeout):
        await client.book(held.hold_id, "k-late")
    # Straight after the timeout the key is claimed but nothing is committed yet.
    assert await client.get_booking_status(idempotency_key="k-late") == BookingStatusOut(
        status="not_found"
    )
    # The same-key retry waits on the claimed key and gets the stored booking. Both
    # timeouts still come from ToolTimeouts, the one table.
    patient = env.client(ToolTimeouts(book=RETRY_BOOK_TIMEOUT_S))
    retry = await book_ok(patient, held.hold_id, "k-late")
    log = await env.inspector.op_log()
    book_rows = rows(log, "book", "k-late")
    (status_row,) = rows(log, "get_booking_status")
    assert [r.outcome for r in book_rows] == ["success", "replayed"]
    original, replay = book_rows
    assert original.done_seq is not None and status_row.done_seq is not None
    assert original.done_seq > status_row.done_seq, "the commit landed after the status check"
    assert original.seq < status_row.seq < replay.seq
    assert (original.done_ts or 0) > status_row.sim_ts
    (booking,) = [b for b in await env.inspector.bookings() if b.created_by_key == "k-late"]
    assert retry.booking_id == booking.booking_id
    status = await client.get_booking_status(idempotency_key="k-late")
    assert isinstance(status, BookingStatusOut) and status.status == "confirmed"


async def test_late_commit_in_process_is_a_timeout_with_the_commit_in_place(env: Env) -> None:
    if env.kind == "http":
        pytest.skip("covered by the real late-commit test above")
    # Under a SimClock the delay is instantaneous, so the commit is already there when
    # the client sees its timeout. The same-key retry still replays it.
    faults = FaultsConfig(commit_latency_ms=CommitLatency(book=9000))
    client = await env.load(forced("success", "success", faults=faults))
    held = await hold_first_free(client)
    with pytest.raises(ToolTimeout):
        await client.book(held.hold_id, "k-late")
    retry = await client.book(held.hold_id, "k-late")
    assert isinstance(retry, BookOut)
    assert [r.outcome for r in rows(await env.inspector.op_log(), "book", "k-late")] == [
        "success",
        "replayed",
    ]


async def test_demo_clock_agent_and_world_agree_after_advance(env: Env) -> None:
    await env.load(clock_mode="system_offset")
    world_now = await env.inspector.now()
    assert abs((env.agent_clock.now() - world_now).total_seconds()) < 1
    advanced = await env.inspector.advance(11 * 60)
    assert advanced - world_now >= timedelta(minutes=11)
    agent_now = env.agent_clock.now()
    world_now = await env.inspector.now()
    assert abs((agent_now - world_now).total_seconds()) < 1
    # Real time keeps flowing in the demo clock.
    assert world_now >= advanced
