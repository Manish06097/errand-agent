"""Pre-fill at ingress and the clear of a forgotten field (gate 2 decisions 10 and 17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.runner import harness
from tests.api.support import OPENER, world

NO_AREA = OPENER  # "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


async def _brief(h: Any) -> dict[str, Any]:
    state = (await h.client.get(f"/api/conversations/{h.conversation_id}")).json()["state"]
    brief: dict[str, Any] = state["errand"]["brief"]
    return brief


async def _save_home(h: Any, cid: str = "a1") -> None:
    response = await h.client.post(
        "/api/memory",
        json={
            "key": "home_area",
            "scope": "global",
            "value": "indiranagar",
            "client_action_id": cid,
        },
    )
    assert response.status_code == 201


async def test_the_area_comes_from_memory_and_is_never_asked(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        await _save_home(h)
        view = await h.say(NO_AREA)
        brief = await _brief(h)
    assert view.card() is None and view.outcome == "PRESENTED_OPTIONS"
    assert brief["area"]["value"] == "indiranagar"
    assert brief["area"]["prov"]["source"] == "memory"
    assert brief["area"]["prov"]["memory_id"] == "m-01"


async def test_without_a_memory_the_area_is_still_asked(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        view = await h.say(NO_AREA)
    card = view.card()
    assert card is not None and "area" in [f.value for f in card.fields]


async def test_a_forget_between_two_messages_clears_the_field_and_the_agent_asks_again(
    tmp_path: Path,
) -> None:
    """S49's shape: the checkpoint still holds the pre-filled area when it is forgotten."""
    async with harness(tmp_path, world()) as h:
        await _save_home(h)
        first = await h.say(NO_AREA)
        assert first.outcome == "PRESENTED_OPTIONS"
        done = await h.client.delete("/api/memory/m-01", params={"client_action_id": "d1"})
        assert done.status_code == 200
        # the stored checkpoint is stale; the state API already hides the field
        assert (await _brief(h))["area"] is None
        second = await h.say("Actually, please search again.")
        records = await h.records()
        brief = await _brief(h)
    assert brief["area"] is None
    ingress_records = [r for r in records.traces[second.run_id] if r["type"] == "invalidate"]
    assert [(r["what"]["brief_field"], r["reason"]) for r in ingress_records] == [
        ("area", "memory_forgotten")
    ]
    card = second.card()
    assert card is not None and "area" in [f.value for f in card.fields]


async def test_a_correct_refills_the_field_from_the_active_successor(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        await _save_home(h)
        await h.say(NO_AREA)
        fixed = await h.client.patch(
            "/api/memory/m-01", json={"value": "koramangala", "client_action_id": "p1"}
        )
        assert fixed.status_code == 200
        await h.say("Please look again.")
        brief = await _brief(h)
    assert brief["area"]["value"] == "koramangala"
    assert brief["area"]["prov"]["memory_id"] == "m-02"
