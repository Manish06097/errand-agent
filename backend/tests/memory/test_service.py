"""The memory service: explicit writes, corrections, suggestions and their 409s."""

from __future__ import annotations

import pytest

from errand.memory.service import MemoryConflict, MemoryNotFound, MemoryRefused, RouteMismatch
from tests.memory.conftest import USER, Env


async def test_an_explicit_write_is_active_with_located_evidence_and_an_event(env: Env) -> None:
    got = await env.remember(
        "home_area",
        "global",
        "indiranagar",
        "Hi, I live in Indiranagar so keep it local.",
        quote="I live in Indiranagar so keep it local.",
    )
    mem = got["memory"]
    assert (mem["memory_id"], mem["status"], mem["kind"], mem["value"]) == (
        "m-01", "active", "explicit", "indiranagar",
    )  # fmt: skip
    assert mem["evidence"]["quote"] == "I live in Indiranagar so keep it local."
    assert mem["source"] == "You told me" and mem["label"] == "indiranagar"
    detail = await env.service.detail(USER, "m-01")
    assert [e["type"] for e in detail["events"]] == ["written"]
    assert await env.epoch() == 1


async def test_a_write_without_the_users_own_cue_is_refused_and_nothing_is_stored(
    env: Env,
) -> None:
    with pytest.raises(MemoryRefused) as err:
        await env.remember("earliest_time", "global", "10:00", "this time I prefer after 10 am")
    assert err.value.reason == "one_off"
    with pytest.raises(MemoryRefused) as err2:
        await env.remember("earliest_time", "global", "10:00", "start after 10 am")
    assert err2.value.reason == "no_cue"
    assert (await env.service.list_grouped(USER))["preferences"] == []
    assert await env.epoch() == 0


async def test_evidence_must_come_from_a_user_message_of_this_user(env: Env) -> None:
    assistant = await env.say("I always want 10 am", role="assistant")
    with pytest.raises(MemoryRefused, match="user message"):
        await env.service.remember_explicit(
            user_id=USER, key="earliest_time", scope="global", value="10:00",
            message_id=assistant, quote="I always want 10 am",
        )  # fmt: skip
    with pytest.raises(MemoryRefused, match="user message"):
        await env.service.remember_explicit(
            user_id=USER, key="earliest_time", scope="global", value="10:00",
            message_id="msg_9999", quote="I always want 10 am",
        )  # fmt: skip


async def test_a_bad_key_scope_or_value_is_a_refusal(env: Env) -> None:
    for key, scope, value in [
        ("colour", "global", "red"),
        ("home_area", "service:plumber", "indiranagar"),
        ("home_area", "global", "atlantis"),
    ]:
        with pytest.raises(MemoryRefused) as err:
            await env.remember(key, scope, value, "I always want this")
        assert err.value.reason == "bad_value"


async def test_a_second_value_for_the_same_key_supersedes_the_first_in_one_transaction(
    env: Env,
) -> None:
    await env.remember("home_area", "global", "indiranagar", "I live in Indiranagar")
    got = await env.remember("home_area", "global", "koramangala", "I moved to Koramangala")
    assert got["memory"]["memory_id"] == "m-02" and got["memory"]["supersedes"] == "m-01"
    old = await env.row("m-01")
    assert (old.status, old.superseded_by) == ("superseded", "m-02")
    assert old.value == "indiranagar"  # History still shows it
    groups = await env.service.list_grouped(USER)
    assert [m["memory_id"] for m in groups["preferences"]] == ["m-02"]
    assert [m["memory_id"] for m in groups["history"]] == ["m-01"]
    # the old evidence span is hidden from the model only
    async with env.db.session() as repos:
        reds = await repos.memories.redactions_for_message("msg_0001")
    assert [(r.start, r.end, r.kind) for r in reds] == [(0, 21, "superseded")]
    types = [e["type"] for e in (await env.service.detail(USER, "m-02"))["events"]]
    assert types == ["corrected"]
    assert [e["type"] for e in (await env.service.detail(USER, "m-01"))["events"]] == [
        "written",
        "superseded",
    ]


async def test_restating_the_saved_value_changes_nothing(env: Env) -> None:
    await env.remember("home_area", "global", "indiranagar", "I live in Indiranagar")
    before = await env.epoch()
    got = await env.remember("home_area", "global", "indiranagar", "I always say Indiranagar")
    assert got["unchanged"] is True and await env.epoch() == before


async def test_earliest_at_or_after_latest_is_refused_in_either_order(env: Env) -> None:
    await env.remember("latest_time", "global", "12:00", "I always want it done by 12:00")
    with pytest.raises(MemoryRefused) as err:
        await env.remember("earliest_time", "global", "12:00", "I always want 12:00 earliest")
    assert err.value.reason == "contradiction"
    ok = await env.remember("earliest_time", "global", "09:00", "I always want 09:00 earliest")
    assert ok["memory"]["status"] == "active"
    with pytest.raises(MemoryRefused):
        await env.remember("latest_time", "global", "08:00", "I always want it done by 08:00")
    # a different scope is a different question
    other = await env.remember(
        "earliest_time", "service:electrician", "15:00", "I always want electricians at 15:00"
    )
    assert other["memory"]["scope"] == "service:electrician"


async def test_preferring_a_provider_replaces_an_avoid_on_it_and_says_so(env: Env) -> None:
    await env.remember("avoid_provider", "service:plumber", ["P7"], "I never want P7 for plumbing")
    got = await env.remember(
        "preferred_provider", "service:plumber", "P7", "I prefer P7 for plumbing"
    )
    assert [r["key"] for r in got["replaced"]] == ["avoid_provider"]
    assert (await env.row("m-01")).status == "superseded"
    assert (await env.row("m-02")).status == "active"


async def test_an_explicit_write_rejects_a_pending_suggestion_for_the_same_key(env: Env) -> None:
    sug = await env.service.suggest(
        user_id=USER, key="latest_time", scope="global", value="12:00",
        rule="latest_time_before_noon", booking_ids=["B1", "B2", "B3"],
    )  # fmt: skip
    assert sug is not None and sug["memory"]["status"] == "pending_confirmation"
    await env.remember("earliest_time", "global", "09:00", "I always want 09:00 earliest")
    assert (await env.row("m-01")).status == "pending_confirmation"  # earliest is not its pair
    # pair in the same scope: an explicit latest write rejects the suggestion
    await env.remember("latest_time", "global", "13:00", "I always want it done by 13:00")
    row = await env.row("m-01")
    assert row.status == "rejected"
    reasons = [e["details"]["reason"] for e in (await env.service.detail(USER, "m-01"))["events"]
               if e["type"] == "rejected"]  # fmt: skip
    assert reasons == ["superseded_by_explicit"]


async def _suggest_provider(env: Env, value: str, scope: str = "service:electrician"):  # type: ignore[no-untyped-def]
    return await env.service.suggest(
        user_id=USER,
        key="preferred_provider",
        scope=scope,
        value=value,
        rule="same_provider_twice",
        booking_ids=["B1", "B2"],
    )


async def test_suggestions_follow_the_gate_rules(env: Env) -> None:
    first = await _suggest_provider(env, "E4")
    assert first is not None and first["declined"] is None
    again = await _suggest_provider(env, "E4")
    assert again is not None and again["declined"] == "already_pending"
    # Dismiss blocklists the value, so inference stops; stating it yourself still works
    await env.service.dismiss(user_id=USER, memory_id="m-01", client_action_id=env.action())
    blocked = await _suggest_provider(env, "E4")
    assert blocked is not None and blocked["declined"] == "blocklisted"
    stated = await env.remember(
        "preferred_provider", "service:electrician", "E4", "I prefer E4 for electricians"
    )
    assert stated["memory"]["status"] == "active"
    # an active row for the same key and scope stops suggestions
    other = await _suggest_provider(env, "E5")
    assert other is not None and other["declined"] == "active_row_exists"
    # another scope is another question
    pair = await _suggest_provider(env, "A4", "service:ac_servicing")
    assert pair is not None and pair["declined"] is None


async def test_an_active_avoid_on_a_provider_stops_preferring_it_but_not_another(
    env: Env,
) -> None:
    await env.remember("avoid_provider", "service:plumber", ["P7"], "I never want P7")
    blocked = await _suggest_provider(env, "P7", "service:plumber")
    assert blocked is not None and blocked["declined"] == "active_row_exists"
    fine = await _suggest_provider(env, "P5", "service:plumber")
    assert fine is not None and fine["declined"] is None


async def test_a_suggestion_needs_the_rule_the_registry_names(env: Env) -> None:
    with pytest.raises(MemoryRefused) as err:
        await env.service.suggest(
            user_id=USER, key="home_area", scope="global", value="indiranagar",
            rule="same_provider_twice", booking_ids=["B1"],
        )  # fmt: skip
    assert err.value.reason == "no_such_rule"


async def test_keep_and_dismiss_are_conditional_and_a_second_click_is_a_409(env: Env) -> None:
    await env.service.suggest(
        user_id=USER, key="latest_time", scope="global", value="12:00",
        rule="latest_time_before_noon", booking_ids=["B1", "B2", "B3"],
    )  # fmt: skip
    kept = await env.service.keep(user_id=USER, memory_id="m-01", client_action_id="k1")
    assert kept["memory"]["status"] == "active" and kept["memory"]["kind"] == "inferred"
    assert kept["memory"]["source"] == "Inferred, confirmed by you"
    assert kept["memory"]["confirmed_at"] is not None
    # same client_action_id: the first answer comes back, nothing else happens
    epoch = await env.epoch()
    assert await env.service.keep(user_id=USER, memory_id="m-01", client_action_id="k1") == kept
    assert await env.epoch() == epoch
    # a new action on a row that is no longer pending gets 409 with the current row
    with pytest.raises(MemoryConflict) as err:
        await env.service.keep(user_id=USER, memory_id="m-01", client_action_id="k2")
    assert err.value.current["status"] == "active"
    with pytest.raises(MemoryConflict):
        await env.service.dismiss(user_id=USER, memory_id="m-01", client_action_id="d1")


async def test_keep_settles_its_key_and_scope_by_rejecting_the_other_pending_rows(
    env: Env,
) -> None:
    """Review round 1: Keep on a row with a pending sibling hit the one-active index and
    answered 409 with the row still pending. A Keep now settles its (key, scope)."""
    for provider in ("E4", "E5", "E6"):
        await _suggest_provider(env, provider)
    await _suggest_provider(env, "P1", "service:plumber")  # another scope: untouched
    before = await env.epoch()

    kept = await env.service.keep(user_id=USER, memory_id="m-02", client_action_id="k1")
    assert kept["memory"]["status"] == "active"
    assert kept["superseded"] == ["m-01", "m-03"]
    assert await env.epoch() == before + 1  # one transaction, one bump

    for memory_id in ("m-01", "m-03"):
        row = await env.service.detail(USER, memory_id)
        assert row["status"] == "rejected"
        last = row["events"][-1]
        assert (last["type"], last["actor"]) == ("rejected", "user")
        assert last["details"]["reason"] == "superseded_by_keep"
    assert (await env.row("m-04")).status == "pending_confirmation"
    # nobody dismissed those values, so nothing is blocklisted
    async with env.db.session() as repos:
        assert await repos.memories.blocklist(USER) == []
    # and they can be suggested again later, once the kept row goes
    again = await _suggest_provider(env, "E4")
    assert again is not None and again["declined"] == "active_row_exists"
    # a replay of the same click returns the first answer and changes nothing
    assert await env.service.keep(user_id=USER, memory_id="m-02", client_action_id="k1") == kept
    assert await env.epoch() == before + 1


async def test_one_client_action_id_cannot_be_reused_for_another_action(env: Env) -> None:
    await env.service.suggest(
        user_id=USER, key="latest_time", scope="global", value="12:00",
        rule="latest_time_before_noon", booking_ids=["B1"],
    )  # fmt: skip
    await env.service.keep(user_id=USER, memory_id="m-01", client_action_id="same")
    with pytest.raises(RouteMismatch):
        await env.service.dismiss(user_id=USER, memory_id="m-01", client_action_id="same")


async def test_undo_rejects_the_row_and_redacts_nothing(env: Env) -> None:
    mid = await env.say("I always want it after 10 am")
    await env.service.remember_explicit(
        user_id=USER, key="earliest_time", scope="global", value="10:00",
        message_id=mid, quote="I always want it after 10 am",
    )  # fmt: skip
    undone = await env.service.undo(user_id=USER, memory_id="m-01", client_action_id="u1")
    assert undone["memory"]["status"] == "rejected"
    assert (await env.body(mid))["text"] == "I always want it after 10 am"
    async with env.db.session() as repos:
        assert await repos.memories.redactions_for_message(mid) == []
    events = (await env.service.detail(USER, "m-01"))["events"]
    assert events[-1]["details"] == {"reason": "undone"}
    with pytest.raises(MemoryConflict):
        await env.service.undo(user_id=USER, memory_id="m-01", client_action_id="u2")


async def test_undoing_a_correction_brings_the_older_value_back(env: Env) -> None:
    await env.remember("home_area", "global", "indiranagar", "I live in Indiranagar")
    await env.remember("home_area", "global", "koramangala", "I moved to Koramangala")
    got = await env.service.undo(user_id=USER, memory_id="m-02", client_action_id="u1")
    assert got["restored"] == ["m-01"]
    assert (await env.row("m-01")).status == "active"
    assert (await env.row("m-02")).status == "rejected"


async def test_remember_from_a_click_is_explicit_with_ui_evidence_and_idempotent(
    env: Env,
) -> None:
    first = await env.service.remember_from_ui(
        user_id=USER, key="budget_ceiling", scope="service:plumber", value=800,
        client_action_id="chip-1",
    )  # fmt: skip
    assert first["memory"]["evidence"] == {
        "type": "ui_action", "client_action_id": "chip-1"
    }  # fmt: skip
    assert first["memory"]["kind"] == "explicit"
    replay = await env.service.remember_from_ui(
        user_id=USER, key="budget_ceiling", scope="service:plumber", value=800,
        client_action_id="chip-1",
    )  # fmt: skip
    assert replay == first
    assert len((await env.service.list_grouped(USER))["preferences"]) == 1


async def test_correct_is_a_new_version_and_only_an_active_row_can_be_corrected(env: Env) -> None:
    await env.remember("budget_ceiling", "service:plumber", 800, "I always cap plumbers at 800")
    fixed = await env.service.correct(
        user_id=USER, memory_id="m-01", value="₹1,000", client_action_id="c1"
    )
    assert fixed["memory"]["value"] == 1000 and fixed["memory"]["supersedes"] == "m-01"
    with pytest.raises(MemoryConflict) as err:
        await env.service.correct(user_id=USER, memory_id="m-01", value=900, client_action_id="c2")
    assert err.value.current["status"] == "superseded"
    with pytest.raises(MemoryNotFound):
        await env.service.correct(user_id=USER, memory_id="m-99", value=900, client_action_id="c3")
    with pytest.raises(MemoryRefused):
        await env.service.correct(
            user_id=USER, memory_id="m-02", value="cheap", client_action_id="c4"
        )


async def test_another_users_row_does_not_exist_for_you(env: Env) -> None:
    await env.remember("language", "global", "hinglish", "I always want Hinglish")
    with pytest.raises(MemoryNotFound):
        await env.service.detail("u_other", "m-01")
    with pytest.raises(MemoryNotFound):
        await env.service.forget(user_id="u_other", memory_id="m-01", client_action_id="f")


async def test_mark_used_moves_last_used_at_without_an_event_or_an_epoch(env: Env) -> None:
    await env.remember("language", "global", "hinglish", "I always want Hinglish")
    epoch = await env.epoch()
    env.tick(5_000)
    await env.service.mark_used(USER, ["m-01"])
    assert (await env.row("m-01")).last_used_at == env.clock[0]
    assert await env.epoch() == epoch
    assert len((await env.service.detail(USER, "m-01"))["events"]) == 1
