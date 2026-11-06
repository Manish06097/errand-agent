"""ScriptedChatModel: stricter than ChatAnthropic on tool_choice, and it really streams."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from errand.llm.anthropic import default_canonicalizer, request_sha256
from errand.llm.errors import ForcedToolChoiceError, ScriptExhausted
from errand.llm.messages import aggregate, message_to_dict, stream_chunks
from errand.llm.scripted import ScriptedChatModel, ScriptedRequest
from tests.llm.support import S4_USAGE, SEARCH_TOOL, text, tool_use

FORCED: list[Any] = [
    "any",
    "search_providers",
    {"type": "any"},
    {"type": "tool", "name": "search_providers"},
]


@pytest.mark.parametrize("choice", FORCED, ids=["any", "tool-name", "dict-any", "dict-tool"])
def test_bind_tools_rejects_every_forced_tool_choice(choice: Any) -> None:
    model = ScriptedChatModel(script=[])
    with pytest.raises(ForcedToolChoiceError, match="forced tool_choice"):
        model.bind_tools([SEARCH_TOOL], tool_choice=choice)


@pytest.mark.parametrize("choice", ["auto", None])
def test_bind_tools_accepts_auto_and_none(choice: Any) -> None:
    model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    bound = model.bind_tools([SEARCH_TOOL], tool_choice=choice)
    assert bound.invoke([HumanMessage("hi")]).text == "Tap Masters can come Tuesday at 09:00."


@pytest.mark.parametrize("choice", [{"type": "any"}, {"type": "tool", "name": "x"}])
def test_a_forced_choice_smuggled_in_at_call_time_is_rejected_too(choice: Any) -> None:
    model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    with pytest.raises(ForcedToolChoiceError):
        model.bind(tool_choice=choice).invoke([HumanMessage("hi")])
    assert model.requests == ()


TOOL_ONLY = AIMessage(
    content=[],
    id="msg_1",
    tool_calls=[
        {
            "name": "search_providers",
            "args": {"service": "plumber", "area": "HSR"},
            "id": "t1",
            "type": "tool_call",
        },
        {"name": "get_provider", "args": {"provider_id": "P2"}, "id": "t2", "type": "tool_call"},
    ],
    usage_metadata=S4_USAGE[0],
    response_metadata={"stop_reason": "tool_use"},
)


@pytest.mark.parametrize(
    "message",
    [
        TOOL_ONLY,
        tool_use(S4_USAGE[0]),
        text(S4_USAGE[1]),
        AIMessage("plain", id="m", usage_metadata=S4_USAGE[1]),
    ],
    ids=["tool-calls-only", "thinking-and-tool-use", "text-block", "string-content"],
)
async def test_streamed_aggregate_equals_the_whole_message(message: AIMessage) -> None:
    chunks = stream_chunks(message)
    assert len(chunks) > 1
    assert message_to_dict(aggregate(chunks)) == message_to_dict(message)

    # Through the real BaseChatModel.astream path, and against the non-streamed call.
    streamed_model = ScriptedChatModel(script=[message])
    invoked_model = ScriptedChatModel(script=[message])
    got = aggregate([c async for c in streamed_model.astream([HumanMessage("x")])])
    whole = await invoked_model.ainvoke([HumanMessage("x")])
    assert message_to_dict(got) == message_to_dict(whole)
    # The scripted model only fills in a message id when the script left it out.
    assert {**message_to_dict(got), "id": message.id} == message_to_dict(message)


async def test_tool_call_arguments_really_arrive_in_pieces() -> None:
    model = ScriptedChatModel(script=[TOOL_ONLY])
    pieces = [
        tc["args"] or ""
        async for chunk in model.astream([HumanMessage("x")])
        for tc in chunk.tool_call_chunks
    ]
    assert len(pieces) == 4  # two calls, each split in two
    assert "".join(pieces[:2]) == '{"service": "plumber", "area": "HSR"}'


def test_every_request_is_recorded_with_its_canonical_form_and_hash() -> None:
    seen: list[ScriptedRequest] = []

    def policy(request: ScriptedRequest) -> AIMessage:
        seen.append(request)
        return text(S4_USAGE[request.n])

    model = ScriptedChatModel(script=policy)
    bound = model.bind_tools([SEARCH_TOOL], tool_choice="auto", strict=True)
    bound.invoke([HumanMessage("first")])
    bound.invoke([HumanMessage("second")])
    assert [r.n for r in model.requests] == [0, 1]
    assert seen == list(model.requests)
    first = model.requests[0]
    assert first.tool_names == ("search_providers",)
    assert first.canonical["messages"] == [{"role": "user", "content": "first"}]
    assert first.canonical["tool_choice"] == {"type": "auto"}
    expected = default_canonicalizer()([HumanMessage("first")], first.kwargs)
    assert first.sha256 == request_sha256(expected)


def test_a_list_script_says_when_it_runs_out() -> None:
    model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    model.invoke([HumanMessage("one")])
    with pytest.raises(ScriptExhausted, match="request #2"):
        model.invoke([HumanMessage("two")])


def test_recorded_requests_do_not_change_when_the_caller_mutates_its_messages(
    tmp_path: Path,
) -> None:
    model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    msgs = [HumanMessage("original")]
    model.invoke(msgs)
    msgs[0].content = "rewritten"
    assert model.requests[0].messages[0].content == "original"
