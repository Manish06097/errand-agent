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


FIND = "Find a plumber for my kitchen tap tomorrow morning, under ₹1,500."


async def _id_of(h: Any, key: str, scope: str) -> str:
    rows = (await h.client.get("/api/memory")).json()["preferences"]
    return str(next(r["memory_id"] for r in rows if r["key"] == key and r["scope"] == scope))


async def _manifests(h: Any) -> list[dict[str, Any]]:
    rec = await h.records()
    return [
        r["manifest"]
        for r in rec.all_records()
        if r["type"] == "context.built" and r["manifest"].get("builder") == "context.builder"
    ]


async def test_a_forgotten_time_bound_drops_the_bound_and_keeps_the_users_day(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world(), memory_seed="memory_demo") as h:
        await h.say(FIND)
        before = (await _brief(h))["window"]
        earliest = await _id_of(h, "earliest_time", "global")
        await h.say("Any luck so far?")  # the next turn's first request sees the bound in the brief
        last = (await _manifests(h))[-1]["memory_included"]
        assert next(i for i in last if i["id"] == earliest)["prefill"] == "window"
        assert before["expr"]["not_before"] is not None
        assert before["bound_memory_ids"] == {"not_before": earliest}
        assert before["prov"]["source"] == "user_msg" and before["prov"]["memory_id"] is None

        done = await h.client.delete(f"/api/memory/{earliest}", params={"client_action_id": "d1"})
        assert done.status_code == 200
        # the open read never shows the forgotten bound's value
        assert (await _brief(h)).get("window") is None or "08:00" not in str(await _brief(h))

        await h.say("And now?")
        after = (await _brief(h))["window"]
        assert after is not None, "the day the user gave must not be asked again"
        assert after["expr"]["phrase"] == before["expr"]["phrase"]
        assert after["expr"]["not_before"] is None and after["bound_memory_ids"] == {}
        assert after["prov"]["source"] == "user_msg"
        manifests = await _manifests(h)
        assert earliest not in {i["id"] for i in manifests[-1]["memory_included"]}


async def test_forgetting_the_avoid_row_leaves_the_prefer_row_and_admits_the_provider(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world(), memory_seed="memory_demo") as h:
        saved = await h.client.post(
            "/api/memory",
            json={"key": "preferred_provider", "scope": "service:plumber", "value": "P3",
                  "client_action_id": "pp"},
        )  # fmt: skip
        assert saved.status_code == 201, saved.text
        await h.say(FIND)
        avoid = await _id_of(h, "avoid_provider", "service:plumber")
        prefer = await _id_of(h, "preferred_provider", "service:plumber")
        pref = (await _brief(h))["provider_pref"]
        assert pref["value"] == {"prefer_id": "P3", "avoid_ids": ["P7"]}
        assert (pref["prefer_memory_id"], pref["avoid_memory_id"]) == (prefer, avoid)

        done = await h.client.delete(f"/api/memory/{avoid}", params={"client_action_id": "d1"})
        assert done.status_code == 200
        shown = (await _brief(h))["provider_pref"]  # the read already drops only that component
        assert shown["value"] == {"prefer_id": "P3", "avoid_ids": []}

        await h.say("Any luck so far?")
        kept = (await _brief(h))["provider_pref"]
        assert kept["value"] == {"prefer_id": "P3", "avoid_ids": []}
        assert (kept["prefer_memory_id"], kept["avoid_memory_id"]) == (prefer, None)
