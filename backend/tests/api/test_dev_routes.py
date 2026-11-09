"""`/api/dev/*` (M7): the dev drawer's proxy to the world. The routes exist only with
ERRAND_DEV=1; without it they are not registered, so the answer is 404, not 403."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from errand.api.app import create_app
from errand.clock import from_ms
from evals.runner import harness
from tests.api.test_approvals import card_of, ops, to_card, world

DEV_PATHS = (
    ("GET", "/api/dev/scenarios"),
    ("POST", "/api/dev/scenario"),
    ("POST", "/api/dev/clock/advance"),
    ("POST", "/api/dev/next-book"),
    ("GET", "/api/dev/bookings"),
    ("POST", "/api/dev/provider-cancel"),
)


def has_dev_routes(app: object) -> bool:
    found = []
    for route in getattr(app, "routes", []):
        inner = getattr(getattr(route, "original_router", None), "routes", [])
        found += [r for r in [route, *inner] if isinstance(r, APIRoute)]
    return any(r.path.startswith("/api/dev") for r in found)


async def test_dev_routes_do_not_exist_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERRAND_DEV", raising=False)
    async with harness(tmp_path, world()) as h:
        statuses = []
        for method, path in DEV_PATHS:
            response = await h.client.request(method, path, json={} if method == "POST" else None)
            statuses.append(response.status_code)
        assert not has_dev_routes(h.app)
    assert statuses == [404] * len(DEV_PATHS)


async def test_dev_routes_exist_with_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRAND_DEV", "1")
    async with harness(tmp_path, world()) as h:
        assert has_dev_routes(h.app)
        meta = (await h.client.get("/api/meta")).json()
        listing = (await h.client.get("/api/dev/scenarios")).json()
    assert meta["dev"] is True
    ids = [s["id"] for s in listing["scenarios"]]
    assert "S01_happy_path_plumber" in ids and "S21_stale_card_after_correction" in ids


async def test_advance_moves_the_agent_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRAND_DEV", "1")
    async with harness(tmp_path, world()) as h:
        before = await h.now_ms()
        response = await h.client.post("/api/dev/clock/advance", json={"seconds": 660})
        after = await h.now_ms()
        bad = await h.client.post("/api/dev/clock/advance", json={"seconds": -5})
    assert response.status_code == 200
    assert after - before == 660_000 and response.json()["now_ms"] == after
    assert bad.status_code == 422


async def test_next_book_times_out_after_success_without_resetting_the_booking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRAND_DEV", "1")
    async with harness(tmp_path, world()) as h:
        card = await to_card(h)
        pushed = await h.client.post("/api/dev/next-book", json={"outcome": "timeout_booked"})
        assert pushed.status_code == 200
        decided = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        view = await h.finish(decided.json()["run_id"])
        rows = await h.op_log()
        bookings = (await h.client.get("/api/dev/bookings")).json()
        seeded = {b["booking_id"] for b in bookings}
        shown = await card_of(h)
    # The world booked, the client never heard: that is what the button simulates.
    assert ops(rows, "book") >= 1
    assert view.outcome is not None and shown["approval"]["status"] in ("consumed", "granted")
    assert any(b["status"] == "confirmed" for b in bookings), seeded


async def test_provider_cancels_a_chosen_booking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRAND_DEV", "1")
    async with harness(tmp_path, world()) as h:
        card = await to_card(h)
        decided = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        await h.finish(decided.json()["run_id"])
        bookings = (await h.client.get("/api/dev/bookings")).json()
        booked = next(b for b in bookings if b["status"] == "confirmed")
        cancelled = await h.client.post(
            "/api/dev/provider-cancel", json={"booking_id": booked["booking_id"]}
        )
        after = (await h.client.get("/api/dev/bookings")).json()
        unknown = await h.client.post("/api/dev/provider-cancel", json={"booking_id": "B-nope"})
        assert from_ms(booked["slot_start"]).year >= 2026
    assert cancelled.status_code == 200
    status = {b["booking_id"]: b["status"] for b in after}
    assert status[booked["booking_id"]] == "cancelled_by_provider"
    assert unknown.status_code == 404


def test_create_app_registers_dev_routes_only_at_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERRAND_DEV", raising=False)
    assert not has_dev_routes(create_app())
    monkeypatch.setenv("ERRAND_DEV", "1")
    assert has_dev_routes(create_app())
