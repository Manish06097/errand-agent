"""The conservative estimator (architecture 13.1): never lower when blocks are added,
counts every replayed byte, and never below a measured count_tokens."""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from errand.llm.tokens import est_msg, est_request, est_tokens, next_call_floor
from errand.usage import Usage
from tests.llm.s5_samples import build_samples
from tests.llm.support import FIXTURES, SPIKE_OUT

S5 = json.loads((FIXTURES / "s5_tokens.json").read_text(encoding="utf-8"))

_text = st.text(max_size=200)
_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=12)
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | _text,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(_ids, inner, max_size=3),
    max_leaves=8,
)
_text_block = st.builds(lambda t: {"type": "text", "text": t}, _text)
_tool_use_block = st.builds(
    lambda i, n, a: {"type": "tool_use", "id": i, "name": n, "input": a},
    _ids,
    _ids,
    st.dictionaries(_ids, _json, max_size=4),
)
_thinking_block = st.builds(
    lambda t, s: {"type": "thinking", "thinking": t, "signature": s},
    _text,
    st.text(min_size=1, max_size=1200),
)
_tool_result_block = st.builds(
    lambda i, c: {"type": "tool_result", "tool_use_id": i, "content": c}, _ids, _text
)
_block = st.one_of(_text_block, _tool_use_block, _thinking_block, _tool_result_block)


def _message(kind: str, blocks: list[str | dict[str, Any]], call_id: str) -> BaseMessage:
    if kind == "ai":
        return AIMessage(content=blocks)
    if kind == "tool":
        return ToolMessage(content=blocks, tool_call_id=call_id)
    return HumanMessage(content=blocks)


_messages = st.lists(
    st.builds(
        _message, st.sampled_from(["ai", "tool", "human"]), st.lists(_block, max_size=3), _ids
    ),
    max_size=5,
)


@settings(max_examples=300, deadline=None)
@given(
    system=st.lists(_text, max_size=2),
    msgs=_messages,
    tools=st.none() | _text,
    target=st.integers(min_value=0, max_value=10),
    new=_block,
)
def test_adding_any_block_never_lowers_the_estimate(
    system: list[str], msgs: list[BaseMessage], tools: str | None, target: int, new: dict[str, Any]
) -> None:
    before = est_request(system, msgs, tools)
    # 1. as a new message of its own
    assert est_request(system, [*msgs, HumanMessage(content=[new])], tools) >= before
    # 2. appended to an existing message's content
    if msgs:
        i = target % len(msgs)
        grown = msgs[i].model_copy(update={"content": [*msgs[i].content, new]})
        assert est_request(system, [*msgs[:i], grown, *msgs[i + 1 :]], tools) >= before
    # 3. as an extra system block
    assert est_request([*system, json.dumps(new)], msgs, tools) >= before


@settings(max_examples=200, deadline=None)
@given(args=st.dictionaries(_ids, _json, max_size=4), call_id=_ids)
def test_an_added_tool_call_never_lowers_the_estimate(args: dict[str, Any], call_id: str) -> None:
    plain = AIMessage(content="Let me search.")
    with_call = AIMessage(
        content="Let me search.",
        tool_calls=[{"name": "search_providers", "args": args, "id": call_id, "type": "tool_call"}],
    )
    assert est_msg(with_call) > est_msg(plain)


def test_thinking_signature_and_tool_use_bytes_are_counted() -> None:
    turn = json.loads((SPIKE_OUT / "s3_turn1.json").read_text())
    real = next(
        m
        for m in turn
        if m["type"] == "AIMessage" and m["content"] and m["content"][0]["type"] == "thinking"
    )
    signature = real["content"][0]["signature"]
    assert len(signature) > 500
    with_thinking = AIMessage(content=real["content"], tool_calls=real["tool_calls"])
    without = AIMessage(content=real["content"][1:], tool_calls=real["tool_calls"])
    # Dropping the thinking block lowers the estimate by at least its signature's share.
    assert est_msg(with_thinking) - est_msg(without) >= est_tokens(signature) - 8

    big_input = {"query": "x" * 3000}
    small = AIMessage(content=[{"type": "tool_use", "id": "t", "name": "n", "input": {}}])
    large = AIMessage(content=[{"type": "tool_use", "id": "t", "name": "n", "input": big_input}])
    assert est_msg(large) - est_msg(small) >= 2000


def test_the_s5_fixture_is_the_spike_generator_output() -> None:
    measured = json.loads((SPIKE_OUT / "s5_tokens.json").read_text())
    rebuilt = build_samples()
    assert set(rebuilt) == set(S5["samples"])
    for name, text in rebuilt.items():
        assert S5["samples"][name]["text"] == text
        assert len(text) == measured[f"claude-opus-5-5:{name}"]["chars"]
        for model, tokens in S5["samples"][name]["tokens"].items():
            assert tokens == measured[f"{model}:{name}"]["tokens"]


def test_the_estimate_is_never_below_the_measured_count() -> None:
    for name, sample in S5["samples"].items():
        estimate = est_request([], [HumanMessage(sample["text"])], None)
        for model, measured in sample["tokens"].items():
            # count_tokens covered the whole one-message request, overhead included.
            assert estimate >= measured, (name, model, estimate, measured)
    # And the tools preamble covers the measured cost of one small tool definition.
    tool = {
        "name": "search_providers",
        "description": "Search providers",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "area": {"type": "string"},
                "date_range": {"type": "string"},
            },
            "required": ["service", "area", "date_range"],
        },
    }
    tools_only = est_request([], [], json.dumps([tool])) - est_request([], [], None)
    assert tools_only >= S5["one_tool_overhead"]


def test_the_floor_is_reported_input_plus_output_plus_what_was_appended() -> None:
    prev = Usage(input_uncached=100, cache_read=2000, cache_write_5m=300, output=450)
    appended = [ToolMessage('{"id":"P1"}', tool_call_id="t1"), HumanMessage("note")]
    assert next_call_floor(prev, appended) == 2400 + 450 + sum(est_msg(m) for m in appended)
    assert next_call_floor(prev, []) == 2850
