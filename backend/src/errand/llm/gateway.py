"""LLMGateway: the only path from agent code to the planner model.

Per call it builds the canonical request, checks the context budget on
max(est_request, next_call_floor) and refuses to send an over-budget request, streams
the response through `astream` (with an optional token callback), aggregates it,
prices it and writes `context.built` and `model.call` trace records.

The planner's tool loop runs through a `LoopSession`, which owns the in-loop message
list. It is append-only, and before every call it checks that the previous request
plus its response is a byte-identical prefix of the next request: thinking signatures
are bound to that prefix (spike C1, C3), so any edit to earlier history must fail here
rather than at the API.

`ERRAND_MODEL_MODE` picks the transport; construction is the same in every mode:
- scripted: a ScriptedChatModel (the caller supplies it);
- replay:   CassetteReplayModel, nothing upstream;
- record:   CassetteRecorder around ChatAnthropic, or around the given transport;
- live:     ChatAnthropic.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import SecretStr

from errand.clock import Clock
from errand.config import LimitSettings, ModelMode, ModelSettings, load_anthropic_credentials
from errand.domain.counters import RunCounters
from errand.llm.anthropic import (
    OFFLINE_API_KEY,
    PLANNER_THINKING,
    PayloadCanonicalizer,
    build_planner,
    canonical_bytes,
    planner_bind_kwargs,
    request_sha256,
)
from errand.llm.cassette import CassetteRecorder, CassetteReplayModel, render_request
from errand.llm.errors import (
    ContextOverBudget,
    LoopPrefixViolation,
    MissingUsage,
    ModelCallLimit,
    ModelDeadline,
    ModelUnavailable,
    check_tool_choice,
)
from errand.llm.messages import aggregate
from errand.llm.pricing import cost_decimal, usage_from_metadata
from errand.llm.scripted import ScriptedChatModel
from errand.llm.tokens import est_request, next_call_floor
from errand.trace.tracer import Tracer
from errand.usage import Usage

TokenCallback = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True)
class ModelTurn:
    message: AIMessage
    usage: Usage
    cost_usd: float
    request_sha256: str
    stop_reason: str | None
    record: Mapping[str, Any]


def _system_texts(system: Any) -> list[str]:
    if system is None:
        return []
    if isinstance(system, str):
        return [system]
    texts: list[str] = []
    for block in system:
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            texts.append(block["text"])
        else:
            texts.append(json.dumps(block, ensure_ascii=False))
    return texts


def _carries_output(chunk: AIMessageChunk) -> bool:
    """True for the first chunk that holds answer text or tool-use input.

    ChatAnthropic's first chunk is message_start, which has no content, so timing the
    first chunk of any kind would measure connection setup. Thinking deltas do not
    count either: they are never shown to the user, and ttft is meant to say how long
    the user waits for the first visible output or the first tool call.
    """
    if chunk.text:
        return True
    if any(tc.get("name") or tc.get("args") for tc in chunk.tool_call_chunks):
        return True
    content = chunk.content
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "tool_use" for block in content
    )


def _response_view(message: AIMessage) -> dict[str, Any]:
    thinking = []
    if isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, Mapping) and block.get("type") == "thinking":
                # The sanitizer replaces the signature with its hash.
                thinking.append(
                    {
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": block.get("signature"),
                    }
                )
    return {
        "text": message.text,
        "tool_calls": [
            {"id": tc.get("id"), "name": tc["name"], "args": tc["args"]}
            for tc in message.tool_calls
        ],
        "thinking": thinking,
    }


class LLMGateway:
    def __init__(
        self,
        *,
        tracer: Tracer,
        tools: Sequence[Any] = (),
        tool_choice: str | Mapping[str, Any] | None = "auto",
        settings: ModelSettings | None = None,
        limits: LimitSettings | None = None,
        transport: BaseChatModel | None = None,
        cassette_path: Path | None = None,
        api_key: SecretStr | None = None,
        counters: RunCounters | None = None,
        clock: Clock | None = None,
    ) -> None:
        # Refused before anything is built, so a forced choice never reaches ChatAnthropic,
        # which would drop it with only a warning when thinking is on (spike C7).
        check_tool_choice(tool_choice)
        self.settings = settings or ModelSettings()
        self.limits = limits or LimitSettings()
        self.tracer = tracer
        # Shared with the run's ToolGateway: one counter object per run (domain.counters).
        self.counters = counters if counters is not None else RunCounters()
        # Only for the time left on the run's deadline, which caps each call (15.1).
        self.clock = clock
        self.mode: ModelMode = self.settings.model_mode

        uses_api = self.mode == "live" or (self.mode == "record" and transport is None)
        if uses_api:
            api_key = api_key or load_anthropic_credentials().anthropic_api_key
            if api_key is None:
                raise RuntimeError(f"ERRAND_MODEL_MODE={self.mode} needs ANTHROPIC_API_KEY")
        self.wire = build_planner(
            self.settings,
            self.limits,
            strict_binding=self.mode in ("live", "record"),
            api_key=api_key if uses_api and api_key is not None else OFFLINE_API_KEY,
        )
        self.canonicalizer = PayloadCanonicalizer(self.wire)
        self.bind_kwargs = planner_bind_kwargs(self.wire, tools)
        self.transport = self._make_transport(transport, cassette_path)
        # Every mode is bound with ChatAnthropic's own tool formatting.
        self._bound = self.transport.bind(**self.bind_kwargs)

    def _make_transport(
        self, transport: BaseChatModel | None, cassette: Path | None
    ) -> BaseChatModel:
        cassette = cassette or self.settings.cassette_path
        if self.mode == "live":
            if transport is not None:
                raise ValueError("live mode always talks to ChatAnthropic; pass no transport")
            return self.wire
        if self.mode in ("replay", "record") and cassette is None:
            raise ValueError(f"ERRAND_MODEL_MODE={self.mode} needs a cassette path")
        if self.mode == "replay":
            if transport is not None:
                raise ValueError("replay mode serves the cassette; pass no transport")
            assert cassette is not None
            return CassetteReplayModel(path=cassette, canonicalizer=self.canonicalizer)
        if self.mode == "record":
            assert cassette is not None
            return CassetteRecorder(
                inner=transport or self.wire, path=cassette, canonicalizer=self.canonicalizer
            )
        if transport is None:
            raise ValueError("scripted mode needs a ScriptedChatModel transport")
        if isinstance(transport, ScriptedChatModel) and transport.canonicalizer is None:
            # Same canonical form as the gateway, so recorded hashes line up.
            transport.canonicalizer = self.canonicalizer
        return transport

    def canonical(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        return self.canonicalizer(messages, self.bind_kwargs)

    def session(
        self,
        *,
        system: str | Sequence[Mapping[str, Any]],
        messages: Sequence[BaseMessage],
        manifest: Mapping[str, Any] | None = None,
    ) -> LoopSession:
        """A loop session. `manifest` is the context builder's account of what went into
        the first request (sections, included messages); it is merged into every
        `context.built` record the session writes."""
        content: Any = system if isinstance(system, str) else [dict(b) for b in system]
        return LoopSession(self, SystemMessage(content=content), messages, manifest)

    async def send(
        self,
        messages: Sequence[BaseMessage],
        canonical: Mapping[str, Any],
        *,
        prev_usage: Usage | None,
        appended: Sequence[BaseMessage],
        on_token: TokenCallback | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> ModelTurn:
        """Budget check, one streamed call, trace. Use LoopSession.call, not this."""
        cap = self.limits.model_calls_per_run
        if self.counters.model_calls >= cap:
            raise ModelCallLimit(cap)
        sha = request_sha256(canonical)
        tools = canonical.get("tools")
        est = est_request(
            _system_texts(canonical.get("system")),
            [m for m in messages if not isinstance(m, SystemMessage)],
            json.dumps(tools, ensure_ascii=False, separators=(",", ":")) if tools else None,
        )
        floor = next_call_floor(prev_usage, appended) if prev_usage is not None else 0
        needed = max(est, floor)
        budget = self.limits.context_budget_tokens
        self.tracer.emit(
            "context.built",
            manifest={
                "builder": "loop_session",
                **(manifest or {}),
                "call": self.counters.model_calls + 1,
                "messages": len(messages),
                "est_request_tokens": est,
                "floor_tokens": floor,
                "budget_tokens": budget,
                "sent": needed <= budget,
                "request_sha256": sha,
            },
        )
        if needed > budget:
            raise ContextOverBudget(needed=needed, est_request=est, floor=floor, budget=budget)

        with self.tracer.opening("model.call", self.settings.planner_model):
            perf = self.tracer.perf
            started = perf()
            timeout_s, capped_by_deadline = self._call_timeout()
            try:
                async with asyncio.timeout(timeout_s):
                    message, usage, first = await self._stream(messages, on_token, perf)
            except Exception as exc:
                self.tracer.emit(
                    "error",
                    where="llm.gateway",
                    exception_type=type(exc).__name__,
                    message=str(exc)[:2000],
                )
                self._record_failed_call(
                    sha=sha,
                    canonical=canonical,
                    started=started,
                    ended=perf(),
                    est=est,
                    floor=floor,
                    budget=budget,
                    exc=exc,
                )
                failure = self._classify(exc, capped_by_deadline)
                if failure is not None:
                    raise failure from exc
                raise
            ended = perf()

            stop_reason = message.response_metadata.get("stop_reason")
            record = self.tracer.model_call(
                model=self.settings.planner_model,
                usage=usage,
                cost=cost_decimal(self.settings.planner_model, usage),
                started=started,
                first_token=first,
                ended=ended,
                mode=self.mode,
                effort=self.settings.planner_effort,
                thinking_display=PLANNER_THINKING["display"],
                request_sha256=sha,
                request=dict(canonical),
                response=_response_view(message),
                stop_reason=stop_reason,
                input_transformations=list(
                    message.response_metadata.get("input_transformations") or []
                ),
                est_request_tokens=est,
                floor_tokens=floor,
                budget_tokens=budget,
            )
        self.counters.model_calls += 1
        self.counters.tokens_in += usage.input_total
        self.counters.tokens_out += usage.output
        self.counters.cost += Decimal(str(record["cost_usd"]))
        return ModelTurn(message, usage, float(record["cost_usd"]), sha, stop_reason, record)

    def _call_timeout(self) -> tuple[float, bool]:
        """The planner call timeout, capped by the run time left (15.1), and whether the
        cap is the deadline's."""
        timeout = self.limits.planner_timeout_s
        if self.clock is None:
            return timeout, False
        left = self.counters.time_left_s(self.clock.now())
        if left is None or left >= timeout:
            return timeout, False
        return max(left, 0.0), True

    async def _stream(
        self,
        messages: Sequence[BaseMessage],
        on_token: TokenCallback | None,
        perf: Callable[[], float],
    ) -> tuple[AIMessage, Usage, float | None]:
        first: float | None = None
        chunks: list[AIMessageChunk] = []
        async for chunk in self._bound.astream(list(messages)):
            if not isinstance(chunk, AIMessageChunk):
                raise TypeError(f"stream yielded {type(chunk).__name__}, not AIMessageChunk")
            if first is None and _carries_output(chunk):
                first = perf()
            chunks.append(chunk)
            if on_token is not None and chunk.text:
                result = on_token(chunk.text)
                if inspect.isawaitable(result):
                    await result
        if not chunks:
            raise RuntimeError("the model stream produced no chunks")
        message = aggregate(chunks)
        if not message.usage_metadata:
            raise MissingUsage("response has no usage_metadata; it cannot be priced")
        return message, usage_from_metadata(message.usage_metadata), first

    @staticmethod
    def _classify(exc: Exception, capped_by_deadline: bool) -> Exception | None:
        """What the SDK raised after its own retries, as a typed failure; None for anything
        else (a 400 or other 4xx is our bug and stays an internal error)."""
        if isinstance(exc, TimeoutError):
            return ModelDeadline() if capped_by_deadline else ModelUnavailable("timeout")
        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            if status == 429 or status >= 500:
                return ModelUnavailable(f"http {status}")
            return None
        if isinstance(exc, anthropic.APIConnectionError):
            # APITimeoutError is a subclass.
            return ModelUnavailable(type(exc).__name__)
        return None

    def _record_failed_call(
        self,
        *,
        sha: str,
        canonical: Mapping[str, Any],
        started: float,
        ended: float,
        est: int,
        floor: int,
        budget: int,
        exc: Exception,
    ) -> None:
        """A call that failed still writes its model.call record: the error, no usage, no cost.
        It counts as a model call, because the request went out."""
        self.tracer.model_call(
            model=self.settings.planner_model,
            usage=Usage(),
            cost=Decimal(0),
            started=started,
            first_token=None,
            ended=ended,
            mode=self.mode,
            effort=self.settings.planner_effort,
            thinking_display=PLANNER_THINKING["display"],
            request_sha256=sha,
            request=dict(canonical),
            response={"text": "", "tool_calls": [], "thinking": []},
            stop_reason=None,
            est_request_tokens=est,
            floor_tokens=floor,
            budget_tokens=budget,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        self.counters.model_calls += 1


def _element_bytes(element: Any) -> bytes:
    return json.dumps(element, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class LoopSession:
    """The planner's in-loop history: append-only, checked byte for byte before each call."""

    def __init__(
        self,
        gateway: LLMGateway,
        system: SystemMessage,
        messages: Sequence[BaseMessage],
        manifest: Mapping[str, Any] | None = None,
    ) -> None:
        for m in messages:
            if isinstance(m, SystemMessage):
                raise ValueError("the system prompt is given once, as `system`")
        self._gateway = gateway
        self._manifest = dict(manifest or {})
        self._messages: list[BaseMessage] = [system, *messages]
        self._expected: dict[str, Any] | None = None  # previous request + response, wire form
        self._prev_usage: Usage | None = None
        self._appended: list[BaseMessage] = []
        self._pending_tool_ids: set[str] = set()
        self._calls = 0
        # Set when a response cannot be carried forward byte for byte; the next call raises.
        self._broken: str | None = None

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        return tuple(self._messages)

    @property
    def pending_tool_call_ids(self) -> frozenset[str]:
        return frozenset(self._pending_tool_ids)

    def append(self, *messages: BaseMessage) -> None:
        """Add tool results (sanitized by the caller first) or a per-call note."""
        for m in messages:
            if isinstance(m, ToolMessage):
                if m.tool_call_id not in self._pending_tool_ids:
                    raise ValueError(f"tool result for unknown or answered call {m.tool_call_id!r}")
                self._pending_tool_ids.discard(m.tool_call_id)
            elif not isinstance(m, HumanMessage):
                raise TypeError(
                    f"only ToolMessage and HumanMessage can be appended, not {type(m).__name__}; "
                    "AIMessages come from call()"
                )
            self._messages.append(m)
            self._appended.append(m)

    async def call(self, *, on_token: TokenCallback | None = None) -> ModelTurn:
        if self._pending_tool_ids:
            raise ValueError(f"unanswered tool calls: {sorted(self._pending_tool_ids)}")
        if not isinstance(self._messages[-1], HumanMessage | ToolMessage):
            raise ValueError("the request must end with a user turn; assistant prefill is not used")
        canonical = self._gateway.canonical(self._messages)
        self._check_prefix(canonical)
        turn = await self._gateway.send(
            self._messages,
            canonical,
            prev_usage=self._prev_usage,
            appended=self._appended,
            on_token=on_token,
            manifest={**self._manifest, "loop_messages": len(self._messages) - 2},
        )
        self._calls += 1
        self._messages.append(turn.message)
        self._expected = self._snapshot(canonical, turn.message)
        self._prev_usage = turn.usage
        self._appended = []
        self._pending_tool_ids = {str(tc["id"]) for tc in turn.message.tool_calls}
        return turn

    def _snapshot(self, canonical: Mapping[str, Any], response: AIMessage) -> dict[str, Any]:
        """The previous request plus `response`, in the form the next request must start with.

        The response's wire form depends on its position (the last assistant turn has
        its text right-stripped), so it is taken from a probe request where something
        follows it. If the probe is not exactly the sent request plus one assistant
        message plus the probe, the response cannot be carried forward byte for byte,
        and the next call() raises rather than checking against less.
        """
        probe = self._gateway.canonical([*self._messages, HumanMessage(content=".")])
        sent = list(canonical["messages"])
        follow = list(probe["messages"])
        fixed = {k: v for k, v in canonical.items() if k != "messages"}
        if (
            len(follow) == len(sent) + 2
            and [_element_bytes(m) for m in follow[: len(sent)]]
            == [_element_bytes(m) for m in sent]
            and follow[len(sent)].get("role") == "assistant"
        ):
            return {**fixed, "messages": follow[: len(sent) + 1]}
        # Seen in practice: ChatAnthropic drops an assistant message with empty content
        # (no text, no tool calls) from any request where it is not last.
        self._broken = (
            f"the response to call #{self._calls} cannot be carried into the next request "
            f"unchanged: the request grew by {len(follow) - len(sent) - 1} message(s) instead "
            f"of 1 (stop_reason={response.response_metadata.get('stop_reason')!r}, "
            f"content={response.content!r}). ChatAnthropic drops an empty assistant turn, "
            "so the loop cannot continue from it; end the run or start a new session"
        )
        return {**fixed, "messages": sent}

    def _check_prefix(self, canonical: Mapping[str, Any]) -> None:
        if self._broken is not None:
            raise LoopPrefixViolation(self._broken)
        expected = self._expected
        if expected is None:
            return
        want = [_element_bytes(m) for m in expected["messages"]]
        got = [_element_bytes(m) for m in canonical["messages"]][: len(want)]
        fixed_want = canonical_bytes({k: v for k, v in expected.items() if k != "messages"})
        fixed_got = canonical_bytes({k: v for k, v in canonical.items() if k != "messages"})
        if want == got and fixed_want == fixed_got:
            return
        actual = {
            **{k: v for k, v in canonical.items() if k != "messages"},
            "messages": list(canonical["messages"])[: len(want)],
        }
        diff = "\n".join(
            difflib.unified_diff(
                render_request(expected),
                render_request(actual),
                fromfile="previous request + response",
                tofile="next request",
                n=2,
                lineterm="",
            )
        )
        raise LoopPrefixViolation(
            "the loop history changed since the previous call; earlier messages, the system "
            "prompt and the tools must stay byte-identical (thinking signatures are bound to "
            f"them):\n{diff}"
        )
