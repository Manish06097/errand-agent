"""Planner construction (architecture 10.1) and the canonical form of a request.

Construction is identical in every model mode; the mode only decides which transport
carries the request. The canonical request is what ChatAnthropic would put on the wire,
so scripted, replay, record and live runs of the same conversation hash the same.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage
from pydantic import SecretStr

from errand.config import LimitSettings, ModelSettings, ReaderSettings

PLANNER_THINKING: Mapping[str, str] = {"type": "adaptive", "display": "summarized"}
READER_THINKING: Mapping[str, str] = {"type": "adaptive"}
STRICT_BINDING: Mapping[str, str] = {"prefix_mismatch_behavior": "error"}
# Scripted and replay modes never send anything, but ChatAnthropic still needs a key
# to exist so its request payload can be computed. This is not a credential.
OFFLINE_API_KEY = SecretStr("offline-no-key")

# Keys that change how the server validates or delivers a request, not what the model
# is asked. They differ between modes on purpose, so the hash leaves them out.
_TRANSPORT_ONLY_KEYS = ("betas", "stream")


def build_planner(
    settings: ModelSettings,
    limits: LimitSettings,
    *,
    strict_binding: bool,
    api_key: SecretStr = OFFLINE_API_KEY,
) -> ChatAnthropic:
    thinking: dict[str, Any] = dict(PLANNER_THINKING)
    betas: list[str] | None = None
    if strict_binding:
        thinking["block_binding"] = dict(STRICT_BINDING)
        betas = [settings.thinking_binding_beta]
    return ChatAnthropic(
        model=settings.planner_model,
        max_tokens=settings.planner_max_tokens,
        thinking=thinking,
        # Effort goes through output_config only, never the `effort` alias.
        output_config={"effort": settings.planner_effort},
        betas=betas,
        anthropic_api_key=api_key,
        default_request_timeout=limits.planner_timeout_s,
        max_retries=settings.planner_max_retries,
    )


def planner_bind_kwargs(wire: ChatAnthropic, tools: Sequence[Any]) -> dict[str, Any]:
    """The request kwargs ChatAnthropic.bind_tools produces for the planner tools.

    Every transport is bound with exactly these, so a scripted model sees the same tool
    schemas and tool_choice that would reach the API.
    """
    if not tools:
        return {}
    bound = wire.bind_tools(tools, tool_choice="auto", strict=True)
    kwargs = dict(bound.kwargs)  # type: ignore[attr-defined]
    # bind_tools passes a tool that is already an Anthropic dict through untouched and
    # silently ignores strict=True for it, so set it here for every custom tool.
    kwargs["tools"] = [
        {**t, "strict": True} if "input_schema" in t and "type" not in t else t
        for t in kwargs["tools"]
    ]
    return kwargs


def build_reader(
    settings: ReaderSettings, *, api_key: SecretStr = OFFLINE_API_KEY
) -> ChatAnthropic:
    """The quarantined review reader (architecture 10.1), built the same in every mode.

    No tools, no tool_choice, no temperature, no prefill and no block binding: Haiku 5.5
    rejects temperature and prefill, and a single-turn request has no thinking history
    to bind. Thinking counts toward max_tokens, so a max_tokens stop is a real failure.
    """
    return ChatAnthropic(
        model=settings.model,
        max_tokens=settings.max_tokens,
        thinking=dict(READER_THINKING),
        # Effort goes through output_config only, never the `effort` alias.
        output_config={"effort": settings.effort},
        anthropic_api_key=api_key,
        default_request_timeout=settings.timeout_s,
        max_retries=settings.max_retries,
    )


# langchain-core pops this before the model sees the call; it is tracing metadata for
# LangSmith, not part of the request, so it never enters the canonical form.
STRUCTURED_METADATA_KEY = "ls_structured_output_format"


def structured_bind_kwargs(wire: ChatAnthropic, schema: type[Any]) -> dict[str, Any]:
    """The kwargs ChatAnthropic.with_structured_output(method="json_schema") binds.

    Every transport is bound with exactly these, so a scripted or replayed reader sees
    the output_config.format a live call would send, and all modes hash the same. The
    parser half of with_structured_output is not used: the caller parses the raw text
    itself, strictly.
    """
    structured = wire.with_structured_output(schema, method="json_schema")
    first = getattr(structured, "first", None)
    kwargs = getattr(first, "kwargs", None)
    if not isinstance(kwargs, Mapping) or "output_config" not in kwargs:
        raise RuntimeError("with_structured_output no longer binds output_config; re-check")
    return dict(kwargs)


def call_kwargs(bind_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """The kwargs that reach the model's generate call (and so the canonical request)."""
    return {k: v for k, v in bind_kwargs.items() if k != STRUCTURED_METADATA_KEY}


# (messages, request kwargs such as tools and tool_choice) -> canonical request
Canonicalizer = Callable[[Sequence[BaseMessage], Mapping[str, Any]], dict[str, Any]]


class PayloadCanonicalizer:
    """Canonical request from ChatAnthropic's own payload builder.

    `_get_request_payload` is private, but the version is pinned and this is the only
    place that calls it. tests/llm/test_construction.py::
    test_canonical_request_is_what_the_sdk_sends_and_hashes_the_same_in_every_mode fails
    if its output stops matching what the SDK sends.
    """

    def __init__(self, wire: ChatAnthropic) -> None:
        self._wire = wire

    def __call__(
        self, messages: Sequence[BaseMessage], kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = self._wire._get_request_payload(list(messages), **dict(kwargs))
        return canonical_from_payload(payload)


def canonical_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    # Top-level keys are sorted, which is also the order the SDK writes them in the
    # request body. Everything below the top level keeps the order it was built in.
    canon = {k: payload[k] for k in sorted(payload) if k not in _TRANSPORT_ONLY_KEYS}
    thinking = canon.get("thinking")
    if isinstance(thinking, Mapping):
        canon["thinking"] = {k: v for k, v in thinking.items() if k != "block_binding"}
    # A JSON round trip deep-copies the payload, so nothing here aliases the caller's
    # message objects, and fails loudly on anything that is not plain JSON.
    result: dict[str, Any] = json.loads(json.dumps(canon, ensure_ascii=False))
    return result


def canonical_bytes(canonical: Mapping[str, Any]) -> bytes:
    # No sort_keys: nested key order is part of the request, as it is on the wire.
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_sha256(canonical: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(canonical)).hexdigest()


def default_canonicalizer() -> PayloadCanonicalizer:
    """The planner canonicalizer for default settings, for fakes used outside a gateway."""
    return PayloadCanonicalizer(
        build_planner(ModelSettings(), LimitSettings(), strict_binding=False)
    )
