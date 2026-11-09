"""BUG-N3-3: a watcher tick that mirrored a booking before `verify` recorded it left the
booking without its origin for good.

The watcher mirrors every upcoming booking the provider lists (14.1), ours included. If a
tick lists the booking in the moment between the world's `book` and verify's own
`known_bookings` write, the mirror row lands with no origin conversation, brief or intent,
and verify's upsert then updated the status and slot but kept those three NULL. The user
never got the "cancelled" line in the chat they booked in, and the replacement search lost
the original brief and the approved price (found by the first browser run of the follow-up
spec, where the watcher polls every second). The repro runs that interleaving through the
API: a tick fires inside verify's status check, before verify writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals.runner import harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


async def test_a_tick_between_book_and_verify_keeps_the_origin(tmp_path: Path) -> None:
    world = ScenarioConfig(
        id="n3_3", seed=7, world=WorldConfig(book=BookConfig(forced=["success", "success"]))
    )
    async with harness(tmp_path, world) as h:
        client = h.manager.world.client
        real = client.get_booking_status
        fired = {"done": False}

        async def get_booking_status(**kwargs: Any) -> Any:
            if not fired["done"]:
                fired["done"] = True
                await h.app.state.watcher.tick()  # the booking exists; verify has not written
            return await real(**kwargs)

        client.get_booking_status = get_booking_status
        await h.say(OPENER)
        convo = await h.conversation()
        card = [m for m in convo["messages"] if m["body"].get("type") == "action"][-1]
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        await h.finish(str(click.json()["run_id"]))
        assert fired["done"]
        async with h.manager.db.session() as repos:
            known = await repos.known_bookings.for_user("u_demo")
        mine = [k for k in known if k.source_intent_id is not None or k.origin_conversation_id]
        assert mine, "verify's record of the booking lost its origin to the watcher's mirror"
        assert mine[0].origin_conversation_id == h.conversation_id
        assert mine[0].origin_brief_json is not None and mine[0].source_intent_id is not None
