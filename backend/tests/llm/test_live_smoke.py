"""One tiny real Opus 5.5 call through the gateway (ERRAND_LIVE=1; costs well under a cent).

Checks the request that actually left the machine (beta header, adaptive summarized
thinking with strict binding, explicit effort, strict tools, auto tool_choice) and our
cost formula against the API's own usage fields, read from the raw stream events
rather than through LangChain.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from tests.llm.support import rig

BETA = "thinking-binding-controls-2026-08-01"


@pytest.mark.live
async def test_one_real_planner_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRAND_MODEL_MODE", "live")
    raw_usage: list[Any] = []
    real_convert = ChatAnthropic._make_message_chunk_from_anthropic_event

    def capture(self: ChatAnthropic, event: Any, **kwargs: Any) -> Any:
        if event.type == "message_delta":
            raw_usage.append(event.usage)
        return real_convert(self, event, **kwargs)

    monkeypatch.setattr(ChatAnthropic, "_make_message_chunk_from_anthropic_event", capture)

    r = rig(tmp_path, mode="live")
    sent: list[httpx2.Request] = []

    async def on_request(request: httpx2.Request) -> None:
        sent.append(request)

    r.gateway.wire._async_client._client.event_hooks["request"].append(on_request)

    session = r.gateway.session(
        system="You are a terse assistant. Answer with one word.",
        messages=[HumanMessage("Reply with the single word OK. Do not call any tool.")],
    )
    turn = await session.call()

    (request,) = sent
    assert BETA in request.headers["anthropic-beta"].split(",")
    assert r.gateway.wire._async_client.max_retries == 2  # planner_max_retries
    body = json.loads(request.content)
    assert body["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "error"},
    }
    assert body["output_config"] == {"effort": "medium"}
    assert body["tool_choice"] == {"type": "auto"}
    assert all(t["strict"] is True for t in body["tools"])

    (usage,) = raw_usage
    # The beta stream's delta usage has no TTL split, so take what it has.
    creation = getattr(usage, "cache_creation", None)
    w5 = getattr(creation, "ephemeral_5m_input_tokens", 0) or 0
    w1 = getattr(creation, "ephemeral_1h_input_tokens", 0) or 0
    if not (w5 or w1):
        w5 = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    # The API's input_tokens is the uncached part only; LangChain adds the cache back.
    expected = (
        Decimal(usage.input_tokens) * 4
        + Decimal(read) * Decimal("0.2")
        + Decimal(w5) * 5
        + Decimal(w1) * 8
        + Decimal(usage.output_tokens) * 20
    ) / Decimal(1_000_000)

    (call,) = r.of_type("model.call")
    assert call["usage"]["input_uncached"] == usage.input_tokens
    assert call["usage"]["output"] == usage.output_tokens
    assert Decimal(str(call["cost_usd"])) == expected.quantize(Decimal("1e-10"))
    assert call["ttft_ms"] is not None and call["ttft_ms"] <= call["latency_ms"]
    real_input = usage.input_tokens + read + w5 + w1
    assert call["est_request_tokens"] >= real_input
    print(f"\nraw message_delta usage: {usage.model_dump()}")
    print(
        f"live smoke: text={turn.message.text!r} stop={turn.stop_reason} "
        f"raw_usage(input={usage.input_tokens}, cache_read={read}, cache_write_5m={w5}, "
        f"cache_write_1h={w1}, output={usage.output_tokens}) "
        f"est_request={call['est_request_tokens']} cost_usd={call['cost_usd']} "
        f"expected={expected} latency_ms={call['latency_ms']:.0f} ttft_ms={call['ttft_ms']:.0f}"
    )
