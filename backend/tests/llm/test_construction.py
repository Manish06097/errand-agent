"""Planner construction (architecture 10.1), checked on the request that goes over the wire.

The live-mode tests run the real ChatAnthropic, the real Anthropic SDK and its real
HTTP client (httpx2, which this SDK version uses instead of httpx); only the transport
under that client is a local fake that captures the request and
answers with a recorded-shape SSE stream. So nothing leaves the machine, and what is
asserted is the actual HTTP request, headers included.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from errand.llm.anthropic import canonical_bytes, canonical_from_payload, request_sha256
from errand.llm.errors import ForcedToolChoiceError, ModelUnavailable
from errand.llm.scripted import ScriptedChatModel
from tests.llm.support import S4_USAGE, SYSTEM, Rig, rig, text

BETA = "thinking-binding-controls-2026-08-01"
DUMMY_KEY = SecretStr("offline-test-key")


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _ok_stream() -> bytes:
    """A text reply whose usage is the spike's call 1 (11 uncached, 1843 written for 5 m)."""
    usage = {
        "input_tokens": 11,
        "cache_creation_input_tokens": 1843,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 1843, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 5,
    }
    return _sse(
        [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {**usage, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "OK"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": usage,
            },
            {"type": "message_stop"},
        ]
    )


class FakeAnthropic:
    """Captures every HTTP request the SDK makes and answers with an SSE stream."""

    def __init__(self, *, overloaded: int = 0) -> None:
        self.requests: list[httpx2.Request] = []
        # The first `overloaded` requests get a 529, which the SDK retries.
        self.overloaded = overloaded

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if len(self.requests) <= self.overloaded:
            return httpx2.Response(
                529,
                # 1 ms, so the SDK's retry wait does not slow the suite down.
                headers={"content-type": "application/json", "retry-after-ms": "1"},
                json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
            )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_ok_stream()
        )

    def body(self, i: int = -1) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[i].content)
        return result

    def install(self, wire: ChatAnthropic) -> None:
        # Keep ChatAnthropic's own SDK client (key, headers, timeout) and swap only the
        # transport underneath its HTTP client.
        client: anthropic.AsyncAnthropic = wire._async_client
        client._client._transport = httpx2.MockTransport(self)


@pytest.fixture
def fake() -> Iterator[FakeAnthropic]:
    yield FakeAnthropic()


def _live(tmp_path: Path, fake: FakeAnthropic, **kwargs: Any) -> Rig:
    r = rig(tmp_path, mode="live", api_key=DUMMY_KEY, **kwargs)
    fake.install(r.gateway.wire)
    return r


def test_scripted_mode_payload_has_the_planner_settings(tmp_path: Path) -> None:
    r = rig(tmp_path, transport=ScriptedChatModel(script=[]))
    canon = r.gateway.canonical([HumanMessage("hi")])
    assert canon["model"] == "claude-opus-5-5"
    assert canon["max_tokens"] == 16_000
    assert canon["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert canon["output_config"] == {"effort": "medium"}
    assert "effort" not in canon and "reasoning_effort" not in canon
    assert canon["tool_choice"] == {"type": "auto"}
    assert [t["strict"] for t in canon["tools"]] == [True]
    assert "betas" not in canon


async def test_live_mode_sends_the_binding_beta_and_strict_settings(
    tmp_path: Path, fake: FakeAnthropic
) -> None:
    r = _live(tmp_path, fake)
    session = r.gateway.session(system=SYSTEM, messages=[HumanMessage("Say OK.")])
    turn = await session.call()

    (request,) = fake.requests
    assert request.url.path == "/v1/messages"
    assert BETA in request.headers["anthropic-beta"].split(",")
    # SDK 2.6 sends the key as a bearer token.
    assert request.headers["authorization"] == "Bearer offline-test-key"
    assert request.headers["x-stainless-read-timeout"] == "60.0"  # planner timeout, 15.1
    body = fake.body()
    assert body["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "error"},
    }
    assert body["output_config"] == {"effort": "medium"}
    assert "effort" not in body
    assert body["max_tokens"] == 16_000
    assert body["tool_choice"] == {"type": "auto"}
    assert all(t["strict"] is True for t in body["tools"])
    assert body["stream"] is True

    # The real SDK's usage, through LangChain, priced with our formula (spike C6).
    assert turn.message.text == "OK"
    (call,) = r.of_type("model.call")
    assert call["usage"]["input_uncached"] == 11
    assert call["usage"]["cache_write_5m"] == 1843
    assert call["cost_usd"] == 0.009359
    assert call["stop_reason"] == "end_turn"
    # Timed at the text delta, not at message_start (which has no content).
    assert call["ttft_ms"] is not None and 0 <= call["ttft_ms"] <= call["latency_ms"]


async def test_planner_retries_are_an_explicit_setting_that_reaches_the_sdk(
    tmp_path: Path,
) -> None:
    fake = FakeAnthropic(overloaded=1)
    r = _live(tmp_path, fake)
    assert r.gateway.settings.planner_max_retries == 1
    assert r.gateway.wire.max_retries == 1
    assert r.gateway.wire._async_client.max_retries == 1

    turn = await r.gateway.session(system=SYSTEM, messages=[HumanMessage("Say OK.")]).call()
    assert turn.message.text == "OK"
    # One 529, then the answer: two HTTP requests inside one model.call. The trace has
    # one record for them, so latency includes the retry (N3 sets the value to 1).
    assert [q.headers["x-stainless-retry-count"] for q in fake.requests] == ["0", "1"]
    assert len(r.of_type("model.call")) == 1
    assert r.gateway.counters.model_calls == 1


async def test_with_retries_off_one_overloaded_answer_fails_the_call(tmp_path: Path) -> None:
    fake = FakeAnthropic(overloaded=1)
    r = _live(tmp_path, fake, model_settings={"planner_max_retries": 0})
    assert r.gateway.wire._async_client.max_retries == 0
    with pytest.raises(ModelUnavailable) as exc:
        await r.gateway.session(system=SYSTEM, messages=[HumanMessage("Say OK.")]).call()
    assert exc.value.reason == "http 529"
    assert isinstance(exc.value.__cause__, anthropic.APIStatusError)
    assert len(fake.requests) == 1
    (err,) = r.of_type("error")
    assert err["where"] == "llm.gateway"
    # The failed call still writes its model.call record: the error, no usage, no cost.
    (call,) = r.of_type("model.call")
    assert "529" in call["error"] and call["cost_usd"] == 0 and call["usage"]["output"] == 0


async def test_canonical_request_is_what_the_sdk_sends_and_hashes_the_same_in_every_mode(
    tmp_path: Path, fake: FakeAnthropic
) -> None:
    messages = [HumanMessage("Find me a plumber in Indiranagar")]
    live = _live(tmp_path / "live", fake)
    await live.gateway.session(system=SYSTEM, messages=messages).call()
    on_the_wire = canonical_from_payload(fake.body())

    scripted_model = ScriptedChatModel(script=[text(S4_USAGE[0])])
    scripted = rig(tmp_path / "scripted", transport=scripted_model)
    await scripted.gateway.session(system=SYSTEM, messages=messages).call()

    # Byte for byte, key order included: the canonical form is the request body minus
    # the transport-only fields (stream, the beta list, block_binding).
    assert canonical_bytes(scripted_model.requests[0].canonical) == canonical_bytes(on_the_wire)
    (live_call,) = live.of_type("model.call")
    (scripted_call,) = scripted.of_type("model.call")
    assert live_call["request_sha256"] == scripted_call["request_sha256"]
    assert live_call["request_sha256"] == request_sha256(on_the_wire)


@pytest.mark.parametrize(
    "choice",
    ["any", "search_providers", {"type": "any"}, {"type": "tool", "name": "search_providers"}],
    ids=["any", "tool-name", "dict-any", "dict-tool"],
)
def test_the_gateway_refuses_a_forced_choice_before_chatanthropic_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: Any
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(ChatAnthropic, "bind_tools", lambda *a, **k: built.append(a))
    real_init = ChatAnthropic.__init__

    def spy_init(self: ChatAnthropic, *args: Any, **kwargs: Any) -> None:
        built.append(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ChatAnthropic, "__init__", spy_init)
    with pytest.raises(ForcedToolChoiceError):
        rig(tmp_path, mode="live", api_key=DUMMY_KEY, tool_choice=choice)
    assert built == []


def test_live_mode_without_a_key_refuses_to_start(tmp_path: Path) -> None:
    # The suite points ERRAND_ENV_FILE at /dev/null and removes ANTHROPIC_API_KEY.
    with pytest.raises(RuntimeError, match="needs ANTHROPIC_API_KEY"):
        rig(tmp_path, mode="live")


def test_replay_and_record_need_a_cassette(tmp_path: Path) -> None:
    for mode, transport in (("replay", None), ("record", ScriptedChatModel(script=[]))):
        with pytest.raises(ValueError, match="cassette"):
            rig(tmp_path / mode, mode=mode, transport=transport)  # type: ignore[arg-type]
