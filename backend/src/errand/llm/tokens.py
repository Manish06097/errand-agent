"""Conservative token estimator (architecture 13.1, ADR 0007).

The spike measured 1.68 characters per token at worst (Hindi-mixed text) and 1.89 for
compact JSON. Dividing UTF-8 bytes by 1.5 can only overcount, because bytes are never
fewer than characters. Every block is counted by the bytes we hold for it, including
thinking blocks with their signatures and tool_use inputs, because all of them are
replayed as input inside the tool loop.

The estimate only ever grows when something is added: every part costs at least 8, and
nothing is deduplicated. An AIMessage's tool calls are counted from `tool_calls` and
again if the content also carries the tool_use block. That double count is small and
keeps the function monotonic.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from errand.usage import Usage

# Fixed cost of the tools preamble on top of the schema bytes; the spike measured 386
# tokens for one small tool (spikes/claude/out/s5_tokens.json, one_tool_overhead).
TOOLS_PREAMBLE_TOKENS = 400


def est_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / 1.5) + 8


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _content_parts(content: str | list[str | dict[str, Any]]) -> Iterable[str]:
    if isinstance(content, str):
        yield content
        return
    for block in content:
        yield block if isinstance(block, str) else _dumps(block)


def est_msg(message: BaseMessage) -> int:
    parts = list(_content_parts(message.content))
    if isinstance(message, AIMessage):
        parts.extend(
            _dumps({"id": tc.get("id"), "name": tc["name"], "input": tc["args"]})
            for tc in message.tool_calls
        )
    if isinstance(message, ToolMessage):
        parts.append(message.tool_call_id)
    return sum(est_tokens(p) for p in parts)


def est_request(
    system: Sequence[str], msgs: Sequence[BaseMessage], tool_schemas_json: str | None
) -> int:
    tools = TOOLS_PREAMBLE_TOKENS + est_tokens(tool_schemas_json) if tool_schemas_json else 0
    return sum(map(est_tokens, system)) + sum(est_msg(m) for m in msgs) + tools


def next_call_floor(prev_usage: Usage, appended: Sequence[BaseMessage]) -> int:
    """Inside a loop, the previous request's reported size plus its output is a floor.

    `appended` is what was added after the previous response (tool results, a note).
    The response itself is covered by `prev_usage.output`, which counts the full
    thinking we must replay even though we only ever see its summary.
    """
    return prev_usage.input_total + prev_usage.output + sum(est_msg(m) for m in appended)
