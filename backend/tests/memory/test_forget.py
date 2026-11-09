"""Forget is final in agent.sqlite (gate 2 decisions 9, 11 to 13)."""

from __future__ import annotations

import json

import pytest

from errand.domain.brief import AreaFact, Brief, BudgetFact, Provenance
from errand.memory.mask import hash_of
from errand.memory.service import MemoryConflict
from errand.store.repos.known_bookings import KnownBooking
from errand.world.contracts import Area
from tests.memory.conftest import CHAT, USER, Env


def _brief(memory_id: str) -> str:
    prov = Provenance(source="memory", memory_id=memory_id, evidence="I live in Indiranagar")
    other = Provenance(source="user_msg", message_id="msg_0001", evidence="under 800 Indiranagar")
    return Brief(
        area=AreaFact(value=Area.INDIRANAGAR, prov=prov, set_at_turn=1),
        budget_max_inr=BudgetFact(value=800, prov=other, set_at_turn=1),
    ).model_dump_json()


async def test_forget_removes_every_plaintext_copy_from_the_database(env: Env) -> None:
    mid = await env.say("Hello there. I live in Indiranagar, and I like it.")
    await env.service.remember_explicit(
        user_id=USER, key="home_area", scope="global", value="indiranagar",
        message_id=mid, quote="I live in Indiranagar",
    )  # fmt: skip
    other = await env.say("Is Indira Nagar far from here?")
    reply = await env.say(
        "",
        role="assistant",
        body={"type": "clarify", "errand_id": "e1", "fields": ["area"],
              "question": "Do you mean Indiranagar?", "options": ["Indiranagar", "Other"]},
    )  # fmt: skip
    safe = await env.say("Book me a plumber under 800")
    # a "Saved" response sitting in client_requests would hold the value too
    await env.service.remember_from_ui(
        user_id=USER, key="home_area", scope="global", value="indiranagar",
        client_action_id="chip-1",
    )  # fmt: skip
    assert "ndiranagar" in await env.dump()

    out = await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    assert out["forgotten"] == ["m-01"] and out["messages_redacted"] >= 3

    dump = await env.dump()
    assert "ndiranagar" not in dump and "ndira nagar" not in dump.lower()
    assert (await env.body(mid))["text"] == "Hello there. [removed at your request], and I like it."
    assert (await env.body(other))["text"] == "Is [removed at your request] far from here?"
    clarify = await env.body(reply)
    assert clarify["question"] == "Do you mean [removed at your request]?"
    assert clarify["options"] == ["[removed at your request]", "Other"]
    assert (await env.body(safe))["text"] == "Book me a plumber under 800"

    row = await env.row("m-01")
    assert (row.status, row.value) == ("forgotten", None)
    assert row.forgotten_at is not None and row.evidence is not None
    assert "quote" not in row.evidence and row.evidence["message_id"] == mid


async def test_forget_writes_the_blocklist_the_hashes_the_event_and_bumps_the_epoch(
    env: Env,
) -> None:
    await env.remember("earliest_time", "global", "10:00", "I always want to start after 10 am")
    before = await env.epoch()
    await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    assert await env.epoch() > before
    async with env.db.session() as repos:
        block = await repos.memories.blocklist(USER)
        hashes = await repos.memories.hashes_for_user(USER)
        events = await repos.memories.events("m-01")
    assert [(k, s, r) for k, s, _, r in block] == [("earliest_time", "global", "forgotten")]
    assert hash_of(env.key, "10 am") in hashes
    assert hash_of(env.key, "I always want to start after 10 am") in hashes
    assert set(hashes.values()) == {"m-01"}
    assert events[-1].type == "forgotten" and events[-1].actor == "user"
    assert "10" not in json.dumps(events[-1].details)


async def test_the_redaction_rows_describe_the_markers_in_the_new_text(env: Env) -> None:
    mid = await env.say("Hi, I always want 10 am please")
    await env.service.remember_explicit(
        user_id=USER, key="earliest_time", scope="global", value="10:00",
        message_id=mid, quote="I always want 10 am",
    )  # fmt: skip
    await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    text = (await env.body(mid))["text"]
    async with env.db.session() as repos:
        reds = await repos.memories.redactions_for_message(mid)
    assert text == "Hi, [removed at your request] please"
    assert [(text[r.start : r.end], r.kind, r.memory_id) for r in reds] == [
        ("[removed at your request]", "forgotten", "m-01")
    ]


async def test_forgetting_a_key_forgets_its_superseded_ancestors_too(env: Env) -> None:
    first = await env.say("I live in Indiranagar")
    await env.service.remember_explicit(
        user_id=USER, key="home_area", scope="global", value="indiranagar",
        message_id=first, quote="I live in Indiranagar",
    )  # fmt: skip
    second = await env.say("I moved to Koramangala")
    await env.service.remember_explicit(
        user_id=USER, key="home_area", scope="global", value="koramangala",
        message_id=second, quote="I moved to Koramangala",
    )  # fmt: skip
    third = await env.say("I've moved to Whitefield")
    await env.service.remember_explicit(
        user_id=USER, key="home_area", scope="global", value="whitefield",
        message_id=third, quote="I've moved to Whitefield",
    )  # fmt: skip

    out = await env.service.forget(user_id=USER, memory_id="m-03", client_action_id="f1")
    assert out["forgotten"] == ["m-03", "m-02", "m-01"] and out["earlier_values"] == 2
    dump = (await env.dump()).lower()
    for place in ("indiranagar", "koramangala", "whitefield"):
        assert place not in dump
    for memory_id in ("m-01", "m-02", "m-03"):
        row = await env.row(memory_id)
        assert (row.status, row.value) == ("forgotten", None)
        events = [e.type for e in await _events(env, memory_id)]
        assert events[-1] == "forgotten"
    # the earlier superseded evidence redaction rows moved with the overwrites
    for message_id in (first, second, third):
        text = (await env.body(message_id))["text"]
        async with env.db.session() as repos:
            reds = await repos.memories.redactions_for_message(message_id)
        for r in reds:
            assert text[r.start : r.end] == "[removed at your request]"


async def _events(env: Env, memory_id: str):  # type: ignore[no-untyped-def]
    async with env.db.session() as repos:
        return await repos.memories.events(memory_id)


async def test_forgetting_twice_is_a_409_with_the_current_row_and_a_replay_is_not(
    env: Env,
) -> None:
    await env.remember("language", "global", "hindi", "I always want Hindi")
    first = await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    assert await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1") == first
    with pytest.raises(MemoryConflict) as err:
        await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f2")
    assert err.value.current["status"] == "forgotten" and err.value.current["value"] is None


async def test_a_pending_suggestion_can_be_forgotten_and_a_forgotten_value_stays_blocked(
    env: Env,
) -> None:
    await env.service.suggest(
        user_id=USER, key="preferred_provider", scope="service:electrician", value="P4",
        rule="same_provider_twice", booking_ids=["B1", "B2"],
    )  # fmt: skip
    await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    again = await env.service.suggest(
        user_id=USER, key="preferred_provider", scope="service:electrician", value="P4",
        rule="same_provider_twice", booking_ids=["B1", "B2", "B3"],
    )  # fmt: skip
    assert again is not None and again["declined"] == "blocklisted"
    # but the user can still say it themselves
    stated = await env.remember(
        "preferred_provider", "service:electrician", "P4", "I prefer P4 for electricians"
    )
    assert stated["memory"]["status"] == "active"


async def test_archived_briefs_lose_the_forgotten_field_and_its_evidence(env: Env) -> None:
    await env.remember("home_area", "global", "indiranagar", "I live in Indiranagar")
    async with env.db.transaction() as repos:
        await repos.conn.execute(
            "INSERT INTO errands (errand_id, conversation_id, kind, phase, brief_json, created_at) "
            "VALUES ('e1', ?, 'book', 'completed', ?, 1)",
            (CHAT, _brief("m-01")),
        )
        await repos.known_bookings.upsert(
            KnownBooking(
                "B1", USER, "P1", "plumber", 1, 2, "confirmed", 3, CHAT, _brief("m-01"), None
            )
        )
    out = await env.service.forget(user_id=USER, memory_id="m-01", client_action_id="f1")
    assert out["briefs_cleared"] == 2
    async with env.db.session() as repos:
        archived = {i: json.loads(b) for _, i, b in await repos.memories.archived_briefs(USER)}
    for brief in archived.values():
        assert brief["area"] is None
        # the unrelated field survives, but its evidence quote is blanked of the value
        assert brief["budget_max_inr"]["value"] == 800
        assert "ndiranagar" not in json.dumps(brief)
    # bookings stay (decision 12): the row and its provider are untouched
    async with env.db.session() as repos:
        assert [b.booking_id for b in await repos.known_bookings.for_user(USER)] == ["B1"]


async def test_a_forget_leaves_other_values_of_other_keys_alone(env: Env) -> None:
    keep = await env.say("I always want Hindi replies")
    await env.service.remember_explicit(
        user_id=USER, key="language", scope="global", value="hindi",
        message_id=keep, quote="I always want Hindi replies",
    )  # fmt: skip
    await env.remember("home_area", "global", "indiranagar", "I live in Indiranagar")
    await env.service.forget(user_id=USER, memory_id="m-02", client_action_id="f1")
    assert (await env.body(keep))["text"] == "I always want Hindi replies"
    assert (await env.row("m-01")).status == "active"
