"""Cassettes: record through the gateway, replay with nothing upstream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from errand.llm.cassette import CassetteReplayModel, load_cassette
from errand.llm.errors import CassetteMiss
from errand.llm.messages import message_to_dict
from errand.llm.scripted import ScriptedChatModel
from tests.llm.support import S4_USAGE, SYSTEM, Rig, rig, text, tool_use

TOOL_RESULT = '[{"id":"P1","name":"Tap Masters","price":1200}]'
# Record-specific envelope and timing fields; everything else must replay identically.
_VOLATILE = {"seq", "ts", "wall_ts", "span_id", "parent_span_id", "latency_ms", "ttft_ms", "mode"}


async def _loop(r: Rig, system: str = SYSTEM) -> list[dict[str, Any]]:
    session = r.gateway.session(system=system, messages=[HumanMessage("Find me a plumber")])
    turns = [await session.call()]
    session.append(ToolMessage(TOOL_RESULT, tool_call_id="toolu_01"))
    turns.append(await session.call())
    return [message_to_dict(t.message) for t in turns]


def _stable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in rec.items() if k not in _VOLATILE} for rec in records]


async def _record(tmp_path: Path) -> tuple[Path, Rig, list[dict[str, Any]]]:
    cassette = tmp_path / "cassettes" / "loop.jsonl"
    upstream = ScriptedChatModel(script=[tool_use(S4_USAGE[0]), text(S4_USAGE[1])])
    recording = rig(tmp_path / "rec", mode="record", transport=upstream, cassette=cassette)
    messages = await _loop(recording)
    assert len(upstream.requests) == 2
    return cassette, recording, messages


async def test_replay_returns_exactly_what_was_recorded(tmp_path: Path) -> None:
    cassette, recording, recorded_messages = await _record(tmp_path)
    assert len(load_cassette(cassette)) == 2

    replaying = rig(tmp_path / "rep", mode="replay", cassette=cassette)
    assert isinstance(replaying.gateway.transport, CassetteReplayModel)
    replayed_messages = await _loop(replaying)

    assert replayed_messages == recorded_messages
    rec_calls = recording.of_type("model.call")
    rep_calls = replaying.of_type("model.call")
    assert _stable(rep_calls) == _stable(rec_calls)
    assert [c["cost_usd"] for c in rep_calls] == [0.009359, 0.0005166]
    assert [c["usage"] for c in rep_calls] == [c["usage"] for c in rec_calls]
    assert [c["mode"] for c in rep_calls] == ["replay", "replay"]
    # Thinking blocks with their signatures come back byte for byte.
    assert replayed_messages[0]["content"][0]["signature"] == "sig-" + "A" * 900


async def test_one_changed_byte_in_the_system_prompt_misses_with_a_readable_diff(
    tmp_path: Path,
) -> None:
    cassette, _, _ = await _record(tmp_path)
    changed = SYSTEM.replace("Rule 3: never book", "Rule 3: never b00k")
    replaying = rig(tmp_path / "rep", mode="replay", cassette=cassette)
    session = replaying.gateway.session(
        system=changed, messages=[HumanMessage("Find me a plumber")]
    )
    with pytest.raises(CassetteMiss) as exc:
        await session.call()
    report = str(exc.value)
    assert str(cassette) in report
    assert "system differs" in report
    lines = report.splitlines()
    assert (
        "-  | Rule 3: never book without an approval that matches provider, slot and price."
        in lines
    )
    assert (
        "+  | Rule 3: never b00k without an approval that matches provider, slot and price."
        in lines
    )
    # Only the changed rule is marked; the other rules are context at most.
    changed_lines = [ln for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert len(changed_lines) == 2


async def test_a_miss_later_in_the_loop_names_the_nearest_recording(tmp_path: Path) -> None:
    cassette, _, _ = await _record(tmp_path)
    replaying = rig(tmp_path / "rep", mode="replay", cassette=cassette)
    session = replaying.gateway.session(system=SYSTEM, messages=[HumanMessage("Find me a plumber")])
    await session.call()
    session.append(ToolMessage(TOOL_RESULT.replace("1200", "1250"), tool_call_id="toolu_01"))
    with pytest.raises(CassetteMiss) as exc:
        await session.call()
    report = str(exc.value)
    assert "nearest recording: #1 of 2, first 2 of 3 messages identical" in report
    assert any(ln.startswith("+") and "1250" in ln for ln in report.splitlines())


async def test_a_recorded_cassette_holds_no_api_key_and_no_headers(tmp_path: Path) -> None:
    key = "sk-" + "ant-" + "api03-" + "k" * 40
    leaky = text(S4_USAGE[0], body=f"Your key is {key}.")
    leaky.response_metadata["headers"] = {"x-api-key": key, "request-id": "req_1"}
    upstream = ScriptedChatModel(script=[leaky])
    cassette = tmp_path / "leak.jsonl"
    r = rig(tmp_path, mode="record", transport=upstream, cassette=cassette)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage(f"my key is {key}")])
    await session.call()

    raw = cassette.read_text()
    assert "sk-" + "ant-" not in raw
    assert '"headers"' not in raw
    assert "x-api-key" not in raw
    (entry,) = load_cassette(cassette)
    assert "[redacted-key]" in json.dumps(entry.request)
    # The key is still the hash of the real request, so replay matches it.
    assert entry.key == upstream.requests[0].sha256


async def test_the_same_request_recorded_twice_replays_in_order(tmp_path: Path) -> None:
    cassette = tmp_path / "twice.jsonl"
    upstream = ScriptedChatModel(
        script=[text(S4_USAGE[0], body="one"), text(S4_USAGE[1], body="two")]
    )
    for i in range(2):
        r = rig(tmp_path / f"rec{i}", mode="record", transport=upstream, cassette=cassette)
        await r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")]).call()
    replay = rig(tmp_path / "rep", mode="replay", cassette=cassette)
    bodies = []
    for _ in range(3):
        turn = await replay.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")]).call()
        bodies.append(turn.message.text)
    assert bodies == ["one", "two", "two"]
