"""ScriptedChatModel: the offline planner for tests and evals.

It is stricter than the real wrapper where the real wrapper is lenient: a forced
tool_choice raises at bind time and at call time (ChatAnthropic only warns and drops
it when thinking is on, spike C7). Every request is recorded with its canonical form
and hash, the same hash a cassette and a `model.call` trace record use.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_anthropic.chat_models import convert_to_anthropic_tool
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, PrivateAttr, SkipValidation

from errand.llm.anthropic import Canonicalizer, default_canonicalizer, request_sha256
from errand.llm.errors import ScriptExhausted, check_tool_choice
from errand.llm.messages import stream_chunks


@dataclass(frozen=True)
class ScriptedRequest:
    """One request the scripted model received."""

    n: int  # 0-based index of the request
    messages: tuple[BaseMessage, ...]
    kwargs: Mapping[str, Any]
    canonical: Mapping[str, Any]
    sha256: str
    tool_names: tuple[str, ...] = field(default=())


Policy = Callable[[ScriptedRequest], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """A chat model driven by a policy callable or a list of responses."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # A list may hold an exception instance in place of a response: that request raises it
    # (an SDK error such as an overloaded 529), so a test reaches the gateway's mapping.
    script: Annotated[Policy | Sequence[AIMessage | Exception], SkipValidation]
    canonicalizer: Canonicalizer | None = None

    _requests: list[ScriptedRequest] = PrivateAttr(default_factory=list)
    _default_canonicalizer: Canonicalizer | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def requests(self) -> tuple[ScriptedRequest, ...]:
        return tuple(self._requests)

    def bind_tools(
        self,
        tools: Sequence[Mapping[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: str | Mapping[str, Any] | None = None,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        check_tool_choice(tool_choice)
        bound: dict[str, Any] = {
            "tools": [convert_to_anthropic_tool(t, strict=strict) for t in tools]
        }
        if tool_choice is not None:
            bound["tool_choice"] = (
                {"type": "auto"} if tool_choice == "auto" else dict(tool_choice)  # type: ignore[arg-type]
            )
        return self.bind(**bound, **kwargs)

    def _record(self, messages: list[BaseMessage], kwargs: Mapping[str, Any]) -> ScriptedRequest:
        # Call-time kwargs can carry a tool_choice that never went through bind_tools.
        check_tool_choice(kwargs.get("tool_choice"))
        canonicalizer = self.canonicalizer
        if canonicalizer is None:
            if self._default_canonicalizer is None:
                self._default_canonicalizer = default_canonicalizer()
            canonicalizer = self._default_canonicalizer
        canonical = canonicalizer(messages, kwargs)
        tools = kwargs.get("tools") or []
        request = ScriptedRequest(
            n=len(self._requests),
            # Deep copies, so a later mutation by the caller cannot rewrite history here.
            messages=tuple(copy.deepcopy(messages)),
            kwargs=copy.deepcopy(dict(kwargs)),
            canonical=canonical,
            sha256=request_sha256(canonical),
            tool_names=tuple(str(t.get("name")) for t in tools if isinstance(t, Mapping)),
        )
        self._requests.append(request)
        return request

    def _respond(self, request: ScriptedRequest) -> AIMessage:
        if callable(self.script):
            message = self.script(request)
        else:
            if request.n >= len(self.script):
                raise ScriptExhausted(
                    f"script has {len(self.script)} responses, request #{request.n + 1} asked "
                    "for one more"
                )
            scripted = self.script[request.n]
            if isinstance(scripted, Exception):
                raise scripted
            message = scripted
        if not isinstance(message, AIMessage):
            raise TypeError(f"script must produce AIMessage, got {type(message).__name__}")
        message = message.model_copy(deep=True)
        if message.id is None:
            message.id = f"msg_scripted_{request.n + 1:04d}"
        return message

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._respond(self._record(messages, kwargs))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._respond(self._record(messages, kwargs))
        for chunk in stream_chunks(message):
            yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = self._respond(self._record(messages, kwargs))
        for chunk in stream_chunks(message):
            gen = ChatGenerationChunk(message=chunk)
            if run_manager is not None:
                await run_manager.on_llm_new_token(gen.text, chunk=gen)
            yield gen


def sdk_failure(kind: str) -> Exception:
    """A real Anthropic SDK exception, as the SDK raises it after its own retries.

    `overloaded` (529), `server_error` (500), `rate_limit` (429), `connection`,
    `timeout`, and `bad_request` (400, our bug). For scripts that must go through the
    gateway's mapping, not around it.
    """
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(code: int) -> Exception:
        response = httpx.Response(code, request=request, json={"error": {"message": kind}})
        cls = {
            400: anthropic.BadRequestError,
            429: anthropic.RateLimitError,
            500: anthropic.InternalServerError,
        }.get(code, anthropic.APIStatusError)
        return cls(kind, response=response, body=None)

    if kind == "overloaded":
        return status(529)
    if kind == "server_error":
        return status(500)
    if kind == "rate_limit":
        return status(429)
    if kind == "bad_request":
        return status(400)
    if kind == "connection":
        return anthropic.APIConnectionError(request=request)
    if kind == "timeout":
        return anthropic.APITimeoutError(request=request)
    raise ValueError(f"unknown SDK failure {kind!r}")
