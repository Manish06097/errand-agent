"""Provider-side dedupe of `book` keys (architecture 5.3, GATE-01 Q1)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from errand.tools.errors import ToolTimeout
from errand.tools.timeouts import ToolTimeouts
from errand.world.contracts import Area, BookError, BookingStatusOut, CancelError, Service
from errand.world.rng import book_outcome, draw
from errand.world.scenario import (
    BookConfig,
    CommitLatency,
    FaultsConfig,
    ScenarioConfig,
    WorldConfig,
)
from tests.contract.conftest import Env
from tests.contract.helpers import WEEK, body, book_ok, hold_first_free, raw, rows, wait_for_outcome

SHORT = ToolTimeouts(book=0.5)


def scenario(*forced: str, seed: int = 5, enforce: bool = True, **faults: object) -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "id": "idem",
            "seed": seed,
            "world": WorldConfig.model_validate(
                {
                    "enforce_idempotency": enforce,
                    "book": BookConfig.model_validate({"forced": list(forced)}),
                }
            ),
            "faults": FaultsConfig.model_validate(faults),
        }
    )


async def test_replay_of_an_applied_key_is_a_no_op(env: Env) -> None:
    # A second forced success makes any re-evaluation visible as a second booking.
    client = await env.load(scenario("success", "success"))
    held = await hold_first_free(client)
    args = {"hold_id": held.hold_id, "idempotency_key": "k-replay"}
    first = await raw(client, "book", args)
    second = await raw(client, "book", args)
    assert first[0] == second[0] == 200
    assert first[1] == second[1], "a replay returns the stored bytes"
    assert body(first[1])["status"] == "confirmed"
    booked = [b for b in await env.inspector.bookings() if b.created_by_key == "k-replay"]
    assert len(booked) == 1
    log = rows(await env.inspector.op_log(), "book", "k-replay")
    assert [r.outcome for r in log] == ["success", "replayed"]
    assert log[1].draw is None and log[1].attempt is None
    assert log[1].response_json == first[1].decode()


async def test_same_key_on_an_in_progress_key_waits_then_replays(env: Env) -> None:
    # The first book's commit is delayed. In-process both tasks are created before
    # either runs, so the second arrives while the first sleeps on the SimClock; over
    # HTTP the second is sent once the world shows the first in progress.
    client = await env.load(
        scenario("success", "success", commit_latency_ms=CommitLatency(book=400))
    )
    held = await hold_first_free(client)
    args = {"hold_id": held.hold_id, "idempotency_key": "k-wait"}
    first = asyncio.create_task(raw(client, "book", args))
    if env.kind == "http":
        await wait_for_outcome(env.inspector, "book", "k-wait", "in_progress")
    second = asyncio.create_task(raw(client, "book", args))
    (s1, b1), (s2, b2) = await asyncio.gather(first, second)
    assert s1 == s2 == 200
    assert b1 == b2
    log = rows(await env.inspector.op_log(), "book", "k-wait")
    assert [r.outcome for r in log] == ["success", "replayed"]
    original, waiter = log
    assert waiter.seq > original.seq
    assert original.done_seq is not None and waiter.done_seq is not None
    assert waiter.done_seq > original.done_seq, "the waiter finished after the original"
    assert len([b for b in await env.inspector.bookings() if b.created_by_key == "k-wait"]) == 1


def key_with_outcome(seed: int, attempt: int, wanted: str) -> str:
    for i in range(1000):
        key = f"k-fresh-{i}"
        if (
            book_outcome(
                draw(seed, "book", key, attempt), WorldConfig().book_probabilities.as_dict()
            )
            == wanted
        ):
            return key
    raise AssertionError("no key found")


async def test_timeout_not_booked_then_same_key_draws_afresh(env: Env) -> None:
    key = key_with_outcome(5, 1, "success")
    client = await env.load(scenario("timeout_not_booked"), timeouts=SHORT)
    held = await hold_first_free(client)
    with pytest.raises(ToolTimeout):
        await client.book(held.hold_id, key)
    assert await client.get_booking_status(idempotency_key=key) == BookingStatusOut(
        status="not_found"
    )
    retry = await book_ok(client, held.hold_id, key)
    log = rows(await env.inspector.op_log(), "book", key)
    assert [r.outcome for r in log] == ["timeout_not_booked", "success"]
    assert log[0].draw is None  # forced
    assert log[1].attempt == 1
    assert log[1].draw == draw(5, "book", key, 1)
    status = await client.get_booking_status(idempotency_key=key)
    assert isinstance(status, BookingStatusOut) and status.booking_id == retry.booking_id


async def test_duplicate_delivery_is_a_no_op_with_dedupe_on(env: Env) -> None:
    client = await env.load(scenario("success", "success", duplicate_delivery="book"))
    held = await hold_first_free(client)
    booked = await book_ok(client, held.hold_id, "k-dup")
    log = rows(await env.inspector.op_log(), "book", "k-dup")
    assert [r.outcome for r in log] == ["success", "replayed"]
    assert log[0].response_json == log[1].response_json
    assert json.loads(log[1].response_json or "{}")["booking_id"] == booked.booking_id
    assert len([b for b in await env.inspector.bookings() if b.created_by_key == "k-dup"]) == 1


async def test_key_reuse_with_different_arguments_is_rejected(env: Env) -> None:
    client = await env.load(scenario("success", "success"))
    first = await hold_first_free(client, index=0)
    other = await hold_first_free(client, index=1)
    await book_ok(client, first.hold_id, "k-reuse")
    assert await client.book(other.hold_id, "k-reuse") == BookError(error="idempotency_key_reuse")
    # Across ops too: the same key on a cancel is a different request.
    booking = next(b for b in await env.inspector.bookings() if b.created_by_key == "k-reuse")
    reuse = CancelError(error="idempotency_key_reuse")
    assert await client.cancel(booking.booking_id, "k-reuse") == reuse
    log = rows(await env.inspector.op_log(), "book", "k-reuse")
    assert [r.outcome for r in log] == ["success", "idempotency_key_reuse"]
    assert len([b for b in await env.inspector.bookings() if b.created_by_key == "k-reuse"]) == 1


async def test_dedupe_off_executes_again_and_status_still_resolves_by_key(env: Env) -> None:
    client = await env.load(scenario("timeout_booked", "success", enforce=False), timeouts=SHORT)
    held = await hold_first_free(client)
    with pytest.raises(ToolTimeout):
        await client.book(held.hold_id, "k-off")
    status = await client.get_booking_status(idempotency_key="k-off")
    assert isinstance(status, BookingStatusOut) and status.status == "confirmed"
    first_booking = status.booking_id
    # A provider that does not dedupe really books again: that is what the agent's
    # wait rule exists for, and INV-02/INV-03 must be able to see it.
    again = await book_ok(client, held.hold_id, "k-off")
    assert again.booking_id != first_booking
    booked = [b for b in await env.inspector.bookings() if b.created_by_key == "k-off"]
    assert len(booked) == 2
    assert {b.slot_start for b in booked} == {booked[0].slot_start}
    log = rows(await env.inspector.op_log(), "book", "k-off")
    assert [r.outcome for r in log] == ["timeout_booked", "success"]
    assert [r.attempt for r in log] == [0, 1]
    # get_booking_status(key) answers with the earliest booking for the key.
    status = await client.get_booking_status(idempotency_key="k-off")
    assert isinstance(status, BookingStatusOut) and status.booking_id == first_booking


async def test_dedupe_off_does_not_wait_on_an_in_progress_key(env: Env) -> None:
    late = CommitLatency(book=400)
    client = await env.load(scenario("success", "success", enforce=False, commit_latency_ms=late))
    held = await hold_first_free(client)
    args = {"hold_id": held.hold_id, "idempotency_key": "k-nowait"}
    first = asyncio.create_task(raw(client, "book", args))
    if env.kind == "http":
        await wait_for_outcome(env.inspector, "book", "k-nowait", "in_progress")
    second = asyncio.create_task(raw(client, "book", args))
    await asyncio.gather(first, second)
    log = rows(await env.inspector.op_log(), "book", "k-nowait")
    assert sorted(r.outcome for r in log) == ["success", "success"]
    assert len([b for b in await env.inspector.bookings() if b.created_by_key == "k-nowait"]) == 2


async def outcomes_by_key(
    env: Env, keys: list[str], *, interleave: bool, reverse: bool
) -> dict[str, str]:
    client = await env.load(ScenarioConfig(id="order", seed=21), timeouts=ToolTimeouts(book=0.3))
    holds = {}
    for i, key in enumerate(keys):
        holds[key] = (await hold_first_free(client, "P9" if i % 2 else "P3", index=i // 2)).hold_id
    for key in reversed(keys) if reverse else keys:
        if interleave:
            await client.search_providers(Service.PLUMBER, Area.INDIRANAGAR, WEEK)
            await client.get_provider("P1")
        with contextlib.suppress(ToolTimeout):
            await client.book(holds[key], key)
    return {r.idempotency_key or "": r.outcome for r in rows(await env.inspector.op_log(), "book")}


async def test_draws_are_keyed_not_ordered(env: Env) -> None:
    keys = [f"k-order-{i}" for i in range(12)]
    plain = await outcomes_by_key(env, keys, interleave=False, reverse=False)
    shuffled = await outcomes_by_key(env, keys, interleave=True, reverse=True)
    assert plain == shuffled
    probs = WorldConfig().book_probabilities.as_dict()
    assert plain == {k: book_outcome(draw(21, "book", k, 0), probs) for k in keys}
    assert len(set(plain.values())) > 1, "the sample should cover more than one outcome"
