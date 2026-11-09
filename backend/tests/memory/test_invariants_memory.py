"""INV-05, INV-06, INV-19 and INV-20 hold on real records and fail on a doctored copy."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evals import invariants
from evals.invariants import Records
from evals.runner import harness
from tests.api.support import OPENER, world

AREA_MESSAGE = "Find a plumber in Indiranagar for my kitchen tap this week, mornings only."


async def real_records(root: Path) -> Records:
    """A run with memory in it: one saved and kept, one forgotten, one suggestion pending."""
    async with harness(root, world()) as h:
        c = h.client
        for n, (key, scope, value) in enumerate(
            [
                ("home_area", "global", "indiranagar"),
                ("language", "global", "hindi"),
                ("budget_ceiling", "service:plumber", 800),
            ]
        ):
            r = await c.post(
                "/api/memory",
                json={"key": key, "scope": scope, "value": value, "client_action_id": f"a{n}"},
            )
            assert r.status_code == 201
        service = h.app.state.memory
        async with h.app.state.manager.db.transaction() as repos:
            from errand.store.repos.known_bookings import KnownBooking

            for i in (1, 2):
                await repos.known_bookings.upsert(
                    KnownBooking(f"B{i}", "u_demo", "P1", "plumber", i, i + 1, "confirmed", i,
                                 None, None, None)
                )  # fmt: skip
        await service.suggest(
            user_id="u_demo", key="preferred_provider", scope="service:plumber", value="P1",
            rule="same_provider_twice", booking_ids=["B1", "B2"],
        )  # fmt: skip
        await h.say(OPENER)
        assert (
            await c.delete("/api/memory/m-02", params={"client_action_id": "d1"})
        ).status_code == 200
        await h.say("Search once more please.")
        return await h.records()


@pytest.fixture
async def rec(tmp_path: Path) -> Records:
    return await real_records(tmp_path / "real")


def fails(name: str, rec: Records) -> list[str]:
    return invariants.INVARIANTS[name](rec)


def test_all_four_hold_on_real_records(rec: Records) -> None:
    for name in ("INV-05", "INV-06", "INV-19", "INV-20"):
        assert fails(name, rec) == [], name
    assert rec.notes["max_estimated_tokens"] > 0


def _model_calls(rec: Records) -> list[dict[str, Any]]:
    return [r for r in rec.all_records() if r["type"] == "model.call"]


# ---------------------------------------------------------------------------- INV-06


def test_inv06_fails_when_an_estimate_is_over_the_budget(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    _model_calls(rec)[0]["est_request_tokens"] = 12_001
    assert any("estimated 12001" in v for v in fails("INV-06", rec))


def test_inv06_fails_when_a_floor_is_over_the_budget(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    _model_calls(rec)[0]["floor_tokens"] = 13_000
    assert fails("INV-06", rec)


def test_inv06_checks_reported_tokens_only_when_they_are_real(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    call = _model_calls(rec)[0]
    call["usage"]["input_uncached"] = 12_500
    assert fails("INV-06", rec) == []  # scripted: the usage is the double's
    call["mode"] = "replay"
    assert any("API reported 12500" in v for v in fails("INV-06", rec))


def test_inv06_recomputes_a_lower_bound_so_the_gateway_cannot_vouch_for_itself(
    rec: Records,
) -> None:
    """Review round 1: INV-06 trusted est_request_tokens, which the code under test writes."""
    rec = copy.deepcopy(rec)
    call = _model_calls(rec)[0]
    recorded = call["est_request_tokens"]
    # a request that really holds 30,000 characters, recorded with its honest-looking estimate
    call["request"]["messages"].append({"role": "user", "content": "x" * 30_000})
    got = fails("INV-06", rec)
    assert any("independent lower bound" in v and "over 12000" in v for v in got), got
    # one that is under the budget but above what the gateway recorded for it
    call["request"]["messages"][-1]["content"] = "x" * 9_000  # 6,000 tokens by the bound
    call["est_request_tokens"] = 100
    got = fails("INV-06", rec)
    assert any("above the recorded estimate of 100" in v for v in got), got
    # honest records are clean: the bound sits under the estimate
    call["est_request_tokens"] = recorded + 6_100
    assert fails("INV-06", rec) == []


def test_inv06_reports_the_biggest_lower_bound_it_computed(rec: Records) -> None:
    assert fails("INV-06", rec) == []
    assert 0 < rec.notes["max_lower_bound_tokens"] <= rec.notes["max_estimated_tokens"]


def test_inv06_fails_on_a_request_sent_after_context_built_said_no(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    call = _model_calls(rec)[0]
    for r in rec.all_records():
        if (
            r["type"] == "context.built"
            and r["manifest"]["request_sha256"] == call["request_sha256"]
        ):
            r["manifest"]["sent"] = False
    assert any("sent although" in v for v in fails("INV-06", rec))


# ---------------------------------------------------------------------------- INV-20


def test_inv20_fails_without_a_context_built_for_a_model_call(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    sha = _model_calls(rec)[0]["request_sha256"]
    for run_id, records in rec.traces.items():
        rec.traces[run_id] = [
            r for r in records
            if not (r["type"] == "context.built" and r["manifest"]["request_sha256"] == sha)
        ]  # fmt: skip
    assert any("0 context.built" in v for v in fails("INV-20", rec))


def test_inv20_fails_when_the_manifest_lists_a_message_the_request_does_not_hold(
    rec: Records,
) -> None:
    rec = copy.deepcopy(rec)
    for r in rec.all_records():
        manifest = r["manifest"] if r["type"] == "context.built" else {}
        if manifest.get("builder") == "context.builder" and manifest["messages_included"]:
            manifest["messages_included"].append("msg_phantom")
            break
    assert any("S6 holds" in v for v in fails("INV-20", rec))


def test_inv20_fails_when_a_listed_message_has_the_wrong_role(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    for r in rec.all_records():
        ids = r["manifest"].get("messages_included") if r["type"] == "context.built" else None
        if ids and len(ids) >= 2:
            ids[0], ids[1] = ids[1], ids[0]
            break
    else:
        pytest.skip("no run has two earlier messages")
    assert any("rendered" in v for v in fails("INV-20", rec))


# ---------------------------------------------------------------------------- INV-05


def _plant(rec: Records, text: str) -> None:
    call = _model_calls(rec)[-1]
    call["request"]["messages"][0]["content"].append({"type": "text", "text": f"note: {text}"})


def test_inv05_fails_when_a_forgotten_canary_reaches_a_request(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_canaries"] = [{"text": "CANARY-FORGOTTEN-7731", "kind": "forgotten"}]
    assert fails("INV-05", rec) == []
    _plant(rec, "the gate code is canary-forgotten-7731.")
    assert any("forgotten canary" in v for v in fails("INV-05", rec))


def test_inv05_finds_a_canary_across_case_and_punctuation(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_canaries"] = [{"text": "CANARY-SUPERSEDED-1101", "kind": "superseded"}]
    _plant(rec, "Canary Superseded 1101")
    assert fails("INV-05", rec)


def test_inv05_lets_an_undone_value_through_only_in_the_users_own_words(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_canaries"] = [{"text": "CANARY-UNDONE-5521", "kind": "undone"}]
    call = _model_calls(rec)[-1]
    blocks = call["request"]["messages"][0]["content"]
    blocks.append(
        {"type": "text", "text": "<recent_turns>\nuser: CANARY-UNDONE-5521\n</recent_turns>"}
    )
    assert fails("INV-05", rec) == []
    blocks.append(
        {"type": "text", "text": "<recent_turns>\nassistant: CANARY-UNDONE-5521\n</recent_turns>"}
    )
    assert fails("INV-05", rec)


def test_inv05_fails_on_a_pending_suggestion_in_s4(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_inactive_pairs"] = [
        {"key": "preferred_provider", "value": "P1", "status": "pending_confirmation"}
    ]
    assert fails("INV-05", rec) == []
    _plant(rec, '<memory>\n[{"preferred_provider":"P1","service":"plumber"}]\n</memory>')
    assert any("is in S4" in v for v in fails("INV-05", rec))


def test_inv05_is_not_vacuous_on_a_seeded_scenario(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_seed"] = "memory_rich_v1"
    assert any("vacuously" in v for v in fails("INV-05", rec))


# ---------------------------------------------------------------------------- INV-19


def _row(rec: Records, memory_id: str) -> dict[str, Any]:
    return next(m for m in rec.memories if m["memory_id"] == memory_id)


def test_inv19_fails_on_an_explicit_row_without_a_cue(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    message = {
        "message_id": "msg_x",
        "role": "user",
        "body": {"type": "text", "text": "start after 10 am"},
    }
    rec.messages.append(message)
    row = _row(rec, "m-01")
    row["evidence_json"] = json.dumps(
        {
            "type": "user_msg",
            "message_id": "msg_x",
            "start": 0,
            "end": 17,
            "quote": "start after 10 am",
        }
    )
    assert any("no standing-preference cue" in v for v in fails("INV-19", rec))


def test_inv19_fails_on_a_one_off_cue_and_on_a_tool_message(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    text = "this time I always want 10 am"
    rec.messages.append(
        {"message_id": "msg_x", "role": "user", "body": {"type": "text", "text": text}}
    )
    row = _row(rec, "m-01")
    row["evidence_json"] = json.dumps(
        {"type": "user_msg", "message_id": "msg_x", "start": 0, "end": len(text), "quote": text}
    )
    assert any("one-off" in v for v in fails("INV-19", rec))
    rec.messages[-1]["role"] = "assistant"
    assert any("not a user message" in v for v in fails("INV-19", rec))


def test_inv19_fails_when_the_one_off_is_in_the_sentence_but_not_the_quote(
    rec: Records,
) -> None:
    """The planner quoted less: 'I prefer after 5 pm' out of 'this time I prefer after 5 pm'."""
    rec = copy.deepcopy(rec)
    text = "this time I prefer after 5 pm"
    quote = "I prefer after 5 pm"
    rec.messages.append(
        {"message_id": "msg_x", "role": "user", "body": {"type": "text", "text": text}}
    )
    row = _row(rec, "m-01")
    start = text.index(quote)
    row["evidence_json"] = json.dumps(
        {
            "type": "user_msg",
            "message_id": "msg_x",
            "start": start,
            "end": start + len(quote),
            "quote": quote,
        }
    )
    assert any("one-off" in v for v in fails("INV-19", rec))
    # a one-off in a different sentence is not a veto
    text = "Book me a plumber today. I prefer after 5 pm"
    rec.messages[-1]["body"]["text"] = text
    start = text.index(quote)
    row["evidence_json"] = json.dumps(
        {
            "type": "user_msg",
            "message_id": "msg_x",
            "start": start,
            "end": start + len(quote),
            "quote": quote,
        }
    )
    assert not any("one-off" in v for v in fails("INV-19", rec))


def test_inv19_fails_on_a_ui_action_that_never_happened(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    _row(rec, "m-01")["evidence_json"] = json.dumps(
        {"type": "ui_action", "client_action_id": "ghost"}
    )
    assert any("never happened" in v for v in fails("INV-19", rec))


def test_inv19_fails_on_an_inferred_row_without_booking_evidence(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    pending = next(m for m in rec.memories if m["kind"] == "inferred")
    pending["evidence_json"] = json.dumps({"type": "rule", "rule": "x", "booking_ids": ["B404"]})
    assert any("does not exist" in v for v in fails("INV-19", rec))
    pending["evidence_json"] = json.dumps({"type": "user_msg"})
    assert any("without rule evidence" in v for v in fails("INV-19", rec))


def test_inv19_fails_when_a_row_left_pending_without_the_user(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    pending = next(m for m in rec.memories if m["kind"] == "inferred")
    pending["status"] = "active"
    assert any("no user confirmation" in v for v in fails("INV-19", rec))
    rec.memory_events.append(
        {
            "memory_id": pending["memory_id"],
            "type": "confirmed",
            "actor": "model",
            "details_json": None,
        }
    )
    assert any("confirmed by model" in v for v in fails("INV-19", rec))


def test_inv19_fails_on_each_part_of_a_forget(rec: Records) -> None:
    base = copy.deepcopy(rec)
    forgotten = _row(base, "m-02")
    assert forgotten["status"] == "forgotten"

    def broken(edit: Any) -> list[str]:
        doctored = copy.deepcopy(base)
        edit(doctored)
        return fails("INV-19", doctored)

    assert any(
        "value is still stored" in v
        for v in broken(lambda r: _row(r, "m-02").update(value_json='"hindi"'))
    )
    assert any("blocklist" in v for v in broken(lambda r: r.memory_blocklist.clear()))
    assert any("surface-form hashes" in v for v in broken(lambda r: r.memory_hashes.clear()))

    def says_it_in_a_message(r: Records) -> None:
        first_user = next(m for m in r.messages if m["role"] == "user")["message_id"]
        _row(r, "m-02")["evidence_json"] = json.dumps(
            {"type": "user_msg", "message_id": first_user, "start": 0, "end": 5}
        )
        r.redactions.clear()

    assert any("no redaction" in v for v in broken(says_it_in_a_message))
    assert any(
        "no ['forgotten'] event" in v
        for v in broken(
            lambda r: r.memory_events.__setitem__(
                slice(None), [e for e in r.memory_events if e["type"] != "forgotten"]
            )
        )
    )
    assert any(
        "quote is still stored" in v
        for v in broken(
            lambda r: _row(r, "m-02").update(
                evidence_json=json.dumps(
                    {"type": "ui_action", "client_action_id": "a1", "quote": "x"}
                )
            )
        )
    )


def test_inv19_fails_on_plaintext_of_a_forgotten_value_left_in_messages(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_canaries"] = [{"text": "CANARY-FORGOTTEN-7731", "kind": "forgotten"}]
    rec.messages.append(
        {
            "message_id": "msg_y",
            "role": "user",
            "body": {"type": "text", "text": "canary forgotten 7731"},
        }
    )
    assert any("plaintext of forgotten" in v for v in fails("INV-19", rec))


def test_inv19_fails_on_plaintext_of_a_forgotten_value_left_in_an_approval(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    rec.notes["memory_canaries"] = [{"text": "CANARY-FORGOTTEN-7731", "kind": "forgotten"}]
    base = {"approval_id": "ap_x", "card_json": "{}", "material_json": "{}"}
    rec.approvals = [{**base, "card_json": '{"brief":[{"value":"Canary-Forgotten-7731"}]}'}]
    assert any("ap_x: plaintext of forgotten" in v for v in fails("INV-19", rec))
    rec.approvals = [{**base, "material_json": '{"note":"canary forgotten 7731"}'}]
    assert any("ap_x: plaintext of forgotten" in v for v in fails("INV-19", rec))


def test_inv20_fails_when_s4_renders_an_id_the_manifest_does_not_list(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    victim = next(
        r
        for r in rec.all_records()
        if r["type"] == "context.built" and r["manifest"].get("memory_included")
    )
    victim["manifest"]["memory_included"].pop()
    assert any("S4 renders" in v for v in fails("INV-20", rec))


def test_inv20_fails_when_s4_renders_a_row_that_was_never_active(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    pending = next(r for r in rec.memories if r["kind"] == "inferred")
    call = next(r for r in _model_calls(rec))
    manifest = next(
        r["manifest"]
        for r in rec.all_records()
        if r["type"] == "context.built"
        and r["manifest"].get("request_sha256") == call["request_sha256"]
    )
    manifest["memory_included"].append({"id": pending["memory_id"], "score": 1.0, "prefill": None})
    pid = pending["memory_id"]
    block = f'<memory>\n[{{"preferred_provider":"P1","id":"{pid}"}}]\n</memory>'
    call["request"]["messages"][0]["content"].append({"type": "text", "text": block})
    assert any("never active" in v or "S4 renders" in v for v in fails("INV-20", rec))


def test_inv20_fails_when_a_later_request_carries_an_older_epoch(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    manifests = [
        r["manifest"]
        for r in rec.all_records()
        if r["type"] == "context.built" and r["manifest"].get("sent")
    ]
    assert len(manifests) >= 2
    manifests[-1]["memory_epoch"] = manifests[0]["memory_epoch"] - 1
    assert any("memory epoch" in v for v in fails("INV-20", rec))


def test_inv20_fails_when_a_rebuild_is_not_at_the_epoch_the_guard_saw(rec: Records) -> None:
    rec = copy.deepcopy(rec)
    run_id = next(iter(rec.traces))
    records = rec.traces[run_id]
    first = next(i for i, r in enumerate(records) if r["type"] == "context.built")
    records.insert(
        first, {"type": "guard.context_stale", "seq": 0, "detail": {"built": 0, "current": 999}}
    )
    assert any("the guard saw 999" in v for v in fails("INV-20", rec))
