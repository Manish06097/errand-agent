"""LoopSession: the in-loop history is append-only and byte-identical between calls.

Thinking signatures are bound to the exact prefix (spike C1), so each of these edits
would be a 400 from the API under prefix_mismatch_behavior "error". The session must
refuse them before anything is sent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from errand.llm.errors import LoopPrefixViolation
from errand.llm.gateway import LoopSession
from errand.llm.scripted import ScriptedChatModel
from tests.llm.support import S4_USAGE, SYSTEM, rig, text, tool_use

TOOL_RESULT = '[{"id":"P1","name":"Tap Masters","price":1200}]'


async def _after_one_tool_call(
    tmp_path: Path, *, extra_responses: int = 1
) -> tuple[LoopSession, ScriptedChatModel]:
    script = [tool_use(S4_USAGE[0]), *[text(S4_USAGE[1]) for _ in range(extra_responses)]]
    model = ScriptedChatModel(script=script)
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("Find me a plumber")])
    await session.call()
    session.append(ToolMessage(TOOL_RESULT, tool_call_id="toolu_01"))
    return session, model


def _edit_user_text(msgs: tuple[BaseMessage, ...]) -> None:
    msgs[1].content = "Find me an electrician"


def _edit_signature(msgs: tuple[BaseMessage, ...]) -> None:
    block = msgs[2].content[0]
    assert isinstance(block, dict)
    block["signature"] = block["signature"][:-1] + "B"


def _reorder_tool_args(msgs: tuple[BaseMessage, ...]) -> None:
    # Same data, different key order: still a different request on the wire.
    ai = msgs[2]
    assert isinstance(ai, AIMessage)
    ai.tool_calls[0]["args"] = dict(reversed(list(ai.tool_calls[0]["args"].items())))


def _edit_system(msgs: tuple[BaseMessage, ...]) -> None:
    msgs[0].content = SYSTEM.replace("Rule 3", "Rule 3!")


def _edit_tool_result_after_append(msgs: tuple[BaseMessage, ...]) -> None:
    # The tool result of the first call, re-sanitized after the response arrived.
    msgs[3].content = TOOL_RESULT.replace("Tap Masters", "[provider]")


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_edit_user_text, "electrician"),
        (_edit_signature, "signature"),
        (_reorder_tool_args, "input"),
        (_edit_system, "Rule 3!"),
    ],
    ids=["user-text", "thinking-signature", "key-order", "system-prompt"],
)
async def test_any_edit_to_earlier_history_fails_before_sending(
    tmp_path: Path, edit: Callable[[tuple[BaseMessage, ...]], None], expected: str
) -> None:
    session, model = await _after_one_tool_call(tmp_path)
    edit(session.messages)
    with pytest.raises(LoopPrefixViolation) as exc:
        await session.call()
    assert expected in str(exc.value)
    assert len(model.requests) == 1


async def test_editing_a_tool_result_that_was_already_sent_fails(tmp_path: Path) -> None:
    session, model = await _after_one_tool_call(tmp_path, extra_responses=2)
    await session.call()  # the tool result is now part of a sent request
    session.append(HumanMessage("Any cheaper option?"))
    _edit_tool_result_after_append(session.messages)
    with pytest.raises(LoopPrefixViolation, match=r"\[provider\]"):
        await session.call()
    assert len(model.requests) == 2


async def test_append_only_loop_with_a_trailing_note_passes(tmp_path: Path) -> None:
    session, model = await _after_one_tool_call(tmp_path, extra_responses=2)
    await session.call()
    # A per-call note goes after the last result; the earlier prefix is untouched.
    session.append(HumanMessage("Note: the user prefers mornings."))
    await session.call()
    assert len(model.requests) == 3
    first, second, third = (r.canonical["messages"] for r in model.requests)
    assert second[: len(first)] == first
    assert third[: len(second)] == second


async def test_trailing_whitespace_in_a_final_text_turn_is_not_a_false_alarm(
    tmp_path: Path,
) -> None:
    # ChatAnthropic right-strips the text of the last assistant turn only, so the same
    # message is serialized differently once something follows it.
    model = ScriptedChatModel(
        script=[text(S4_USAGE[0], body="Let me check.\n\n"), text(S4_USAGE[1])]
    )
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    await session.call()
    session.append(HumanMessage("go on"))
    await session.call()
    assert len(model.requests) == 2


async def test_the_session_refuses_what_the_api_would_refuse(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[tool_use(S4_USAGE[0])])
    r = rig(tmp_path, transport=model)
    with pytest.raises(ValueError, match="system prompt"):
        r.gateway.session(
            system=SYSTEM, messages=[*r.gateway.session(system="x", messages=[]).messages]
        )
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    await session.call()
    with pytest.raises(TypeError, match="AIMessages come from call"):
        session.append(AIMessage("forged"))
    with pytest.raises(ValueError, match="unknown or answered"):
        session.append(ToolMessage("{}", tool_call_id="toolu_other"))
    with pytest.raises(ValueError, match="unanswered tool calls"):
        await session.call()
    prefill: Any = r.gateway.session(
        system=SYSTEM, messages=[HumanMessage("hi"), AIMessage("Sure")]
    )
    with pytest.raises(ValueError, match="prefill"):
        await prefill.call()
    assert len(model.requests) == 1


@pytest.mark.parametrize("content", ["", [], [{"type": "text", "text": ""}]], ids=repr)
async def test_an_empty_response_cannot_be_carried_forward_so_the_next_call_raises(
    tmp_path: Path, content: Any
) -> None:
    # ChatAnthropic drops an assistant turn with no text and no tool calls from any
    # request where it is not last, so the next request would not start with the
    # previous request plus its response. The session must not quietly check less.
    empty = AIMessage(
        content=content,
        usage_metadata=S4_USAGE[1],
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5"},
    )
    model = ScriptedChatModel(script=[empty, text(S4_USAGE[1])])
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])

    turn = await session.call()  # the empty answer itself is returned and traced
    assert turn.stop_reason == "end_turn"
    assert len(r.of_type("model.call")) == 1

    probe = r.gateway.canonical([*session.messages, HumanMessage("go on")])
    assert [m["role"] for m in probe["messages"]] == ["user", "user"]  # the turn is gone

    session.append(HumanMessage("go on"))
    with pytest.raises(LoopPrefixViolation, match="empty assistant turn"):
        await session.call()
    assert len(model.requests) == 1
