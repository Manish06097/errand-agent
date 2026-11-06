"""A scripted tool loop through the real gateway, tracer and JSONL sink.

The usage numbers are the spike's real Opus 5.5 calls (spikes/claude/out/s4_usage_cache.json):
call 1 wrote 1843 tokens to the 5 minute cache, call 2 read them back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from errand.llm.errors import ContextOverBudget
from errand.llm.scripted import ScriptedChatModel
from tests.llm.support import S4_USAGE, SYSTEM, rig, text, tool_use

TOOL_RESULT = '[{"id":"P1","name":"Tap Masters","price":1200,"slots":["2026-11-10T09:00+05:30"]}]'


async def test_two_call_loop_traces_usage_and_cost_from_the_spike(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[tool_use(S4_USAGE[0]), text(S4_USAGE[1])])
    r = rig(tmp_path, transport=model)
    r.tracer.start_run(trigger="user_message")
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("Find me a plumber")])

    first = await session.call()
    assert [tc["name"] for tc in first.message.tool_calls] == ["search_providers"]
    session.append(ToolMessage(TOOL_RESULT, tool_call_id="toolu_01"))
    second = await session.call()
    assert second.stop_reason == "end_turn"
    r.tracer.end_run(outcome="ANSWERED")
    r.tracer.close()

    calls = r.of_type("model.call")
    assert len(calls) == 2
    assert calls[0]["usage"] == {
        "input_uncached": 11,
        "cache_read": 0,
        "cache_write_5m": 1843,
        "cache_write_1h": 0,
        "output": 5,
        "reasoning": 0,
    }
    # (11 x 4 + 1843 x 4 x 1.25 + 5 x 20) / 1e6
    assert calls[0]["cost_usd"] == 0.009359
    assert calls[1]["usage"] == {
        "input_uncached": 12,
        "cache_read": 1843,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "output": 5,
        "reasoning": 0,
    }
    # (12 x 4 + 1843 x 4 x 0.05 + 5 x 20) / 1e6
    assert calls[1]["cost_usd"] == 0.0005166
    assert [c["stop_reason"] for c in calls] == ["tool_use", "end_turn"]

    # The hash in the trace is the hash of what the scripted model actually received.
    assert [c["request_sha256"] for c in calls] == [req.sha256 for req in model.requests]

    (end,) = r.of_type("run.end")
    totals = end["totals"]
    assert totals["model_calls"] == 2
    assert totals["tokens_in"] == sum(
        sum(
            c["usage"][k]
            for k in ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h")
        )
        for c in calls
    )
    assert totals["tokens_out"] == sum(c["usage"]["output"] for c in calls)
    assert totals["cost_usd"] == round(sum(c["cost_usd"] for c in calls), 10) == 0.0098756
    assert r.gateway.counters.model_calls == 2
    assert r.gateway.counters.cost_usd == totals["cost_usd"]

    built = r.of_type("context.built")
    assert len(built) == 2
    assert all(b["manifest"]["sent"] for b in built)
    # Every model.call hangs off the run span, with seq strictly increasing.
    seqs = [rec["seq"] for rec in r.records()]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_synthetic_one_hour_cache_write_is_priced_at_2x(tmp_path: Path) -> None:
    # SYNTHETIC: no spike call wrote a 1 h cache entry, so this usage is made up to
    # exercise the 2x term. 20 uncached + 1000 written for 1 h, 10 out.
    synthetic: dict[str, Any] = {
        "input_tokens": 1020,
        "output_tokens": 10,
        "total_tokens": 1030,
        "input_token_details": {
            "cache_read": 0,
            "cache_creation": 0,
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 1000,
        },
        "output_token_details": {"reasoning": 0},
    }
    model = ScriptedChatModel(script=[text(synthetic)])
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    turn = await session.call()
    (call,) = r.of_type("model.call")
    assert call["usage"]["cache_write_1h"] == 1000
    assert call["usage"]["input_uncached"] == 20
    # (20 x 4 + 1000 x 4 x 2 + 10 x 20) / 1e6
    assert call["cost_usd"] == turn.cost_usd == 0.00828


async def test_floor_from_reported_usage_refuses_the_next_call_before_sending(
    tmp_path: Path,
) -> None:
    # The model reports far more input than our estimate could see (replayed thinking
    # counts as input, and we only hold its summary). The floor must win.
    heavy: dict[str, Any] = {
        "input_tokens": 11_900,
        "output_tokens": 150,
        "total_tokens": 12_050,
        "input_token_details": {"cache_read": 0, "cache_creation": 0},
        "output_token_details": {"reasoning": 120},
    }
    model = ScriptedChatModel(script=[tool_use(heavy), text(S4_USAGE[1])])
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("Find me a plumber")])
    await session.call()
    session.append(ToolMessage(TOOL_RESULT, tool_call_id="toolu_01"))

    with pytest.raises(ContextOverBudget) as exc:
        await session.call()

    assert exc.value.floor > 12_000
    assert exc.value.est_request < 12_000  # the estimate alone would have let it through
    assert exc.value.needed == exc.value.floor
    assert len(model.requests) == 1  # nothing was sent for the refused call
    assert len(r.of_type("model.call")) == 1
    refused = r.of_type("context.built")[-1]["manifest"]
    assert refused["sent"] is False
    assert refused["floor_tokens"] == exc.value.floor
    assert r.gateway.counters.model_calls == 1


async def test_an_oversized_first_request_is_never_sent(tmp_path: Path) -> None:
    model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    r = rig(tmp_path, transport=model)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("x" * 20_000)])
    with pytest.raises(ContextOverBudget) as exc:
        await session.call()
    assert exc.value.floor == 0
    assert exc.value.est_request > 12_000
    assert model.requests == ()
    assert r.of_type("model.call") == []


async def test_token_callback_gets_text_deltas_but_never_thinking(tmp_path: Path) -> None:
    reply = text(S4_USAGE[1], body="Tap Masters can come Tuesday at 09:00.")
    reply.content = [
        {"type": "thinking", "thinking": "SECRET-REASONING", "signature": "s" * 40},
        *reply.content,
    ]
    model = ScriptedChatModel(script=[reply])
    r = rig(tmp_path, transport=model)
    tokens: list[str] = []

    async def on_token(delta: str) -> None:
        tokens.append(delta)

    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    turn = await session.call(on_token=on_token)
    assert "".join(tokens) == "Tap Masters can come Tuesday at 09:00."
    assert "SECRET-REASONING" not in "".join(tokens)
    assert turn.message.content == reply.content


async def test_a_failing_model_call_is_traced_as_an_error(tmp_path: Path) -> None:
    def broken(_: Any) -> Any:
        raise ConnectionError("upstream reset")

    r = rig(tmp_path, transport=ScriptedChatModel(script=broken))
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    with pytest.raises(ConnectionError):
        await session.call()
    (err,) = r.of_type("error")
    assert err["where"] == "llm.gateway"
    assert err["exception_type"] == "ConnectionError"
    assert r.of_type("model.call") == []
    assert r.gateway.counters.model_calls == 0


class _Ticks:
    """A fake perf clock that the model advances by one second per streamed chunk."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TickingModel(ScriptedChatModel):
    ticks: _Ticks

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        async for gen in super()._astream(*args, **kwargs):
            self.ticks.now += 1.0
            yield gen


@pytest.mark.parametrize(
    ("reply", "first_output_chunk"),
    [
        # empty opening chunk, then the text block
        (text(S4_USAGE[1]), 2),
        # empty opening chunk, a thinking block (not output), then the tool_use block
        (tool_use(S4_USAGE[0]), 3),
    ],
    ids=["text", "thinking-then-tool-use"],
)
async def test_ttft_starts_at_the_first_chunk_with_output_not_the_empty_opener(
    tmp_path: Path, reply: AIMessage, first_output_chunk: int
) -> None:
    ticks = _Ticks()
    model = _TickingModel(script=[reply], ticks=ticks)
    r = rig(tmp_path, transport=model, perf=ticks)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")])
    await session.call()

    (call,) = r.of_type("model.call")
    # Chunk n is yielded at t = n seconds; the call started at t = 0.
    assert call["ttft_ms"] == first_output_chunk * 1000
    assert call["latency_ms"] == ticks.now * 1000
    assert call["latency_ms"] > call["ttft_ms"]


async def test_a_response_with_no_visible_output_has_no_ttft(tmp_path: Path) -> None:
    reply = AIMessage(
        content=[{"type": "thinking", "thinking": "nothing to say", "signature": "s" * 40}],
        usage_metadata=S4_USAGE[1],
        response_metadata={"stop_reason": "max_tokens", "model_name": "claude-opus-5-5"},
    )
    r = rig(tmp_path, transport=ScriptedChatModel(script=[reply]))
    await r.gateway.session(system=SYSTEM, messages=[HumanMessage("hi")]).call()
    (call,) = r.of_type("model.call")
    assert call["ttft_ms"] is None
