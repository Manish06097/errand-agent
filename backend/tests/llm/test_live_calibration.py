"""The estimator never undercounts `count_tokens` on thinking-bearing tool loops
(ERRAND_LIVE=1; two to three small real calls, a few cents at most).

Architecture 13.1 and ADR 0007: the gateway refuses a request over 12,000 estimated
tokens, so the estimate is only a safe gate if it is never below what the API counts.
Replayed thinking blocks (with their signatures) and tool_use inputs count as input inside
a loop, and a summarized thinking block is much smaller than the block that must be
replayed. This test makes a real tool loop, and at every step compares `est_request` with
`messages.count_tokens` on the request that would be sent next.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from errand.config import ModelSettings, load_anthropic_credentials
from errand.llm.tokens import est_request

SYSTEM = "You arrange home-service errands. Use the tools; never answer from memory.\n" + "\n".join(
    f"Rule {i}: be brief and never invent providers." for i in range(8)
)
TOOLS: Any = [
    {
        "name": "search_providers",
        "description": "Search home-service providers by service and area.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}, "area": {"type": "string"}},
            "required": ["service", "area"],
        },
    },
    {
        "name": "get_provider",
        "description": "Open one provider by id.",
        "input_schema": {
            "type": "object",
            "properties": {"provider_id": {"type": "string"}},
            "required": ["provider_id"],
        },
    },
]
PROVIDERS = [
    {
        "id": f"P{i}",
        "name": f"Provider {i}",
        "price_inr": 900 + 50 * i,
        "rating": 4.0 + (i % 10) / 10,
        "slots": [f"2026-11-1{i % 5}T0{7 + i % 3}:00+05:30" for _ in range(3)],
        "languages": ["English", "Kannada"],
    }
    for i in range(1, 9)
]


def run_tool(name: str, args: dict[str, Any]) -> str:
    if name == "search_providers":
        return json.dumps({"providers": PROVIDERS}, ensure_ascii=False)
    return json.dumps({**PROVIDERS[0], "digest": {"positive": 7, "negative": 1}})


def as_langchain(system: str, history: list[dict[str, Any]]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for message in history:
        content = message["content"]
        if message["role"] == "user" and isinstance(content, str):
            out.append(HumanMessage(content=content))
        elif message["role"] == "assistant":
            calls = [
                {"id": b["id"], "name": b["name"], "args": b["input"], "type": "tool_call"}
                for b in content
                if b["type"] == "tool_use"
            ]
            out.append(AIMessage(content=content, tool_calls=calls))
        else:
            for block in content:
                out.append(ToolMessage(content=block["content"], tool_call_id=block["tool_use_id"]))
    return out


@pytest.mark.live
def test_the_estimate_is_never_below_count_tokens_on_thinking_tool_loops() -> None:
    key = load_anthropic_credentials().anthropic_api_key
    assert key is not None, "no ANTHROPIC_API_KEY in the environment or the repo .env"
    client = anthropic.Anthropic(api_key=key.get_secret_value())
    model = ModelSettings().planner_model
    history: Any = [
        {
            "role": "user",
            "content": "Find a plumber in Indiranagar for a dripping tap with search_providers, "
            "then open the first result with get_provider.",
        }
    ]
    rows: list[tuple[int, int, int]] = []
    tools_json = json.dumps(TOOLS, ensure_ascii=False, separators=(",", ":"))
    thinking: Any = {"type": "adaptive", "display": "summarized"}
    thinking_seen = False
    for step in range(4):
        counted = client.messages.count_tokens(
            model=model, system=SYSTEM, messages=history, tools=TOOLS
        ).input_tokens
        estimate = est_request([SYSTEM], as_langchain(SYSTEM, history), tools_json)
        rows.append((step, estimate, counted))
        assert estimate >= counted, f"step {step}: estimated {estimate} < counted {counted}"
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            system=SYSTEM,
            messages=history,
            tools=TOOLS,
            thinking=thinking,
        )
        blocks = [b.model_dump(exclude_none=True) for b in response.content]
        thinking_seen = thinking_seen or any(b["type"] == "thinking" for b in blocks)
        if response.stop_reason != "tool_use":
            break
        history.append({"role": "assistant", "content": blocks})
        history.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": run_tool(b["name"], b["input"]),
                    }
                    for b in blocks
                    if b["type"] == "tool_use"
                ],
            }
        )
    assert len(rows) >= 2, "the model never used a tool, so no loop was measured"
    print("\nlive calibration (step, est_request, count_tokens):")
    for step, estimate, counted in rows:
        print(f"  step {step}: est={estimate} counted={counted} ratio={estimate / counted:.2f}")
    print(f"thinking blocks replayed: {thinking_seen}")
    assert thinking_seen, "no thinking block came back; the fixture did not exercise thinking"
