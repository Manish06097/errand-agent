"""AIMessage helpers shared by the fakes, the cassettes and the gateway.

`stream_chunks` turns a whole message into the chunks a streaming model would emit,
such that `aggregate(stream_chunks(m))` gives back `m`. GenericFakeChatModel cannot
stream a tool-call-only message (spike s7), so our fakes use this instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import reduce
from typing import Any, cast

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.tool import ToolCallChunk, tool_call_chunk
from langchain_core.messages.utils import message_chunk_to_message


def message_to_dict(message: AIMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "name": message.name,
        "content": message.content,
        "tool_calls": [dict(tc) for tc in message.tool_calls],
        "invalid_tool_calls": [dict(tc) for tc in message.invalid_tool_calls],
        "usage_metadata": dict(message.usage_metadata) if message.usage_metadata else None,
        "response_metadata": message.response_metadata,
        "additional_kwargs": message.additional_kwargs,
    }


def message_from_dict(data: Mapping[str, Any]) -> AIMessage:
    return AIMessage(
        id=data.get("id"),
        name=data.get("name"),
        content=data["content"],
        tool_calls=list(data.get("tool_calls") or []),
        invalid_tool_calls=list(data.get("invalid_tool_calls") or []),
        usage_metadata=data.get("usage_metadata"),
        response_metadata=dict(data.get("response_metadata") or {}),
        additional_kwargs=dict(data.get("additional_kwargs") or {}),
    )


def _halves(text: str) -> list[str]:
    if len(text) < 2:
        return [text]
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


def stream_chunks(message: AIMessage) -> list[AIMessageChunk]:
    """Chunks that aggregate back to `message`: an empty opening chunk, text in pieces,
    one chunk per block, tool call arguments split across two chunks, usage and stop
    reason on the last."""
    content = message.content
    empty: str | list[str | dict[str, Any]] = "" if isinstance(content, str) else []
    pieces: list[str | list[str | dict[str, Any]]]
    if isinstance(content, str):
        pieces = cast("list[str | list[str | dict[str, Any]]]", _halves(content))
    else:
        pieces = [[block] for block in content] or [[]]

    # A content-free opening chunk, like the one ChatAnthropic yields for message_start,
    # so code that times or filters chunks meets the same shape offline as live.
    chunks = [
        AIMessageChunk(
            id=message.id,
            content=empty,
            name=message.name,
            additional_kwargs=dict(message.additional_kwargs),
        )
    ]
    chunks += [AIMessageChunk(id=message.id, content=piece) for piece in pieces]

    calls: list[tuple[str, str | None, str | None]] = [
        (tc["name"], json.dumps(tc["args"], ensure_ascii=False), tc.get("id"))
        for tc in message.tool_calls
    ]
    calls += [
        (tc.get("name") or "", tc.get("args") or "", tc.get("id"))
        for tc in message.invalid_tool_calls
    ]
    for index, (name, args, call_id) in enumerate(calls):
        first, *rest = _halves(args or "")
        parts: list[ToolCallChunk] = [
            tool_call_chunk(name=name, args=first, id=call_id, index=index)
        ]
        parts += [tool_call_chunk(name=None, args=r, id=None, index=index) for r in rest]
        chunks += [
            AIMessageChunk(id=message.id, content=empty, tool_call_chunks=[p]) for p in parts
        ]

    chunks.append(
        AIMessageChunk(
            id=message.id,
            content=empty,
            usage_metadata=message.usage_metadata,
            response_metadata=dict(message.response_metadata),
            chunk_position="last",
        )
    )
    return chunks


def aggregate(chunks: Iterable[AIMessageChunk]) -> AIMessage:
    merged = reduce(lambda a, b: a + b, chunks)
    result = message_chunk_to_message(merged)
    if not isinstance(result, AIMessage):  # pragma: no cover - chunk types are fixed
        raise TypeError(f"expected an AIMessage, got {type(result).__name__}")
    return result
