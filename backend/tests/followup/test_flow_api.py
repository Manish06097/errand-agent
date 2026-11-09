"""The follow-up through the real API (architecture 14): the watcher, delivery, the bell and
the user's event stream, the cancel-card row of the policy, and a job resumed after a crash."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from errand.clock import to_ms
from errand.domain.inbound import SystemEvent
from errand.store.repos.approvals import ApprovalRow
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals.runner import Harness, harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def world() -> ScenarioConfig:
    return ScenarioConfig(
        id="followup_api", seed=7, world=WorldConfig(book=BookConfig(forced=["success"] * 3))
    )


async def book_one(h: Harness) -> str:
    """One verified booking through the real path. Returns its world booking id."""
    await h.say(OPENER)
    convo = await h.conversation()
    card = [m for m in convo["messages"] if m["body"].get("type") == "action"][-1]
    click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    await h.finish(str(click.json()["run_id"]))
    return [b for b in await h.inspector.bookings() if b.created_by_key is not None][-1].booking_id


@asynccontextmanager
async def booked(tmp_path: Path) -> AsyncIterator[tuple[Harness, str]]:
    async with harness(tmp_path, world()) as h:
        yield h, await book_one(h)


async def test_a_cancel_card_open_for_the_booking_is_invalidated_and_nothing_is_offered(
    tmp_path: Path,
) -> None:
    """Policy row 3, through the watcher: the cancel card for the booking the provider just
    cancelled is invalidated with booking_no_longer_active in the tick's transaction, and the
    notice says there is nothing to cancel, with no search and no hold."""
    async with booked(tmp_path) as (h, booking_id):
        now = to_ms(h.manager.clock.now())
        material = {
            "v": 1,
            "user_id": "u_demo",
            "action": "cancel",
            "booking_id": booking_id,
            "provider_id": "P1",
            "slot_start": "2026-11-10T09:00:00+05:30",
            "booking_status": "confirmed",
        }
        async with h.manager.db.session() as repos:
            await repos.approvals.insert(
                ApprovalRow(
                    approval_id="ap_cancel",
                    conversation_id=h.conversation_id,
                    errand_id="e_x",
                    proposal_id="pr_cancel",
                    action="cancel",
                    fingerprint="fp1_x",
                    material_json=json.dumps(material),
                    card_json="{}",
                    bound_hold_id=None,
                    status="pending",
                    granted_via=None,
                    granted_message_id=None,
                    invalidated_reason=None,
                    consumed_by_intent=None,
                    requested_at=now,
                    granted_at=None,
                    consumed_at=None,
                    expires_at=now + 600_000,
                )
            )
        searches = len([r for r in await h.op_log() if r["op"] == "search_providers"])
        holds = len([r for r in await h.op_log() if r["op"] == "hold_slot"])
        await h.inspector.fire_provider_cancel(booking_id)
        result = await h.app.state.watcher.tick()
        await h.manager.wait_idle()
        async with h.manager.db.session() as repos:
            row = await repos.approvals.get("ap_cancel")
        assert row is not None and row.status == "invalidated"
        assert row.invalidated_reason == "booking_no_longer_active"
        h.conversations["f"] = f"cf_{result.delivered[0]}"
        await h.switch("f")
        text = json.dumps((await h.conversation())["messages"])
        assert "nothing for you to cancel" in text
        ops = await h.op_log()
        assert len([r for r in ops if r["op"] == "search_providers"]) == searches
        assert len([r for r in ops if r["op"] == "hold_slot"]) == holds


async def test_the_bell_and_the_event_stream(tmp_path: Path) -> None:
    async with booked(tmp_path) as (h, booking_id):
        # The stream announces nothing for a quiet bell and replays what a client missed.
        await h.inspector.fire_provider_cancel(booking_id)
        await h.app.state.watcher.tick()
        await h.manager.wait_idle()
        bell = (await h.client.get("/api/notifications")).json()
        assert bell["unread"] == 1 and len(bell["notifications"]) == 1
        entry = bell["notifications"][0]
        assert entry["title"].endswith("replacement ready") and entry["conversation_id"].startswith(
            "cf_"
        )
        stream = await h.client.get("/api/users/me/events", params={"once": "true"})
        assert "event: notification" in stream.text and f"id: {entry['seq']}" in stream.text
        replay = await h.client.get(
            "/api/users/me/events",
            params={"once": "true"},
            headers={"Last-Event-ID": str(entry["seq"])},
        )
        assert "event: notification" not in replay.text
        read = await h.client.post(f"/api/notifications/{entry['notification_id']}/read")
        assert read.json()["unread"] == 0
        assert (await h.client.post("/api/notifications/nope/read")).status_code == 404


async def test_a_job_resumed_after_a_crash_is_delivered_once(tmp_path: Path) -> None:
    """The run that delivers a job dies before respond: the job is still detected, the next
    tick delivers it, and exactly one notification exists (the reconciler's case)."""
    async with booked(tmp_path) as (h, booking_id):
        await h.inspector.fire_provider_cancel(booking_id)
        watcher = h.app.state.watcher
        calls = {"n": 0}
        real = watcher.start

        async def flaky(conversation_id: str, event: SystemEvent) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the process died before the run started")
            await real(conversation_id, event)

        watcher.start = flaky
        await watcher.tick()
        async with h.manager.db.session() as repos:
            assert [j.state for j in await repos.followups.in_state("detected")] == ["detected"]
        await watcher.tick()
        await watcher.tick()
        await h.manager.wait_idle()
        async with h.manager.db.session() as repos:
            assert await repos.notifications.count() == 1
            assert [j.state for j in await repos.followups.in_state("notified")] == ["notified"]


async def test_a_second_deliverer_finding_the_job_notified_does_nothing(tmp_path: Path) -> None:
    async with booked(tmp_path) as (h, booking_id):
        await h.inspector.fire_provider_cancel(booking_id)
        first = await h.app.state.watcher.tick()
        await h.manager.wait_idle()
        again = await h.app.state.watcher.tick()
        assert first.delivered and not again.delivered and not again.changed
        # The run itself also refuses: a stale start event writes nothing but its notice.
        job_id = first.delivered[0]
        conversation_id = f"cf_{job_id}"
        await h.manager.submit_system(
            conversation_id, SystemEvent(kind="followup_start", job_id=job_id)
        )
        await h.manager.wait_idle()
        async with h.manager.db.session() as repos:
            assert await repos.notifications.count() == 1
