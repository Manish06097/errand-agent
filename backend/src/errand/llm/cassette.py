"""Record and replay model exchanges, keyed by the sha256 of the canonical request.

A cassette is a JSONL file, one exchange per line:
    {"v": 1, "key": <sha256>, "request": <canonical request>, "response": <AIMessage>}

The recorder wraps any chat model (scripted in tests, ChatAnthropic for `make record`).
Replay returns the stored message unchanged: content blocks, tool_calls,
usage_metadata and response_metadata. A miss raises with a unified diff against the
nearest recording, so a prompt change shows up as the line that changed.

Stored requests and responses are scrubbed of API keys and anything header-like; the
key is computed before scrubbing, so it still matches the live request.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import threading
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_anthropic.chat_models import convert_to_anthropic_tool
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import ConfigDict, PrivateAttr

from errand.llm.anthropic import Canonicalizer, request_sha256
from errand.llm.errors import CassetteMiss, check_tool_choice
from errand.llm.messages import aggregate, message_from_dict, message_to_dict, stream_chunks

CASSETTE_VERSION = 1
_API_KEY = re.compile("sk-" + r"ant-[A-Za-z0-9_\-]+")
_SECRET_KEYS = frozenset(
    {"headers", "authorization", "x-api-key", "api_key", "anthropic_api_key", "set-cookie"}
)
_SIGNATURE_KEYS = frozenset({"signature", "data"})


def scrub(value: Any) -> Any:
    """Remove API keys and header-like fields, recursively."""
    if isinstance(value, str):
        return _API_KEY.sub("[redacted-key]", value)
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items() if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


@dataclass(frozen=True)
class CassetteEntry:
    index: int
    key: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]


def load_cassette(path: Path) -> list[CassetteEntry]:
    if not path.is_file():
        return []
    entries: list[CassetteEntry] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("v") != CASSETTE_VERSION:
            raise ValueError(f"{path}:{lineno}: unsupported cassette version {data.get('v')!r}")
        entries.append(CassetteEntry(len(entries), data["key"], data["request"], data["response"]))
    return entries


# ----------------------------------------------------------------------------- diff


def _render(value: Any, path: str, out: list[str]) -> None:
    leaf = path.rsplit(".", 1)[-1]
    if isinstance(value, Mapping):
        if not value:
            out.append(f"{path}: {{}}")
        for k, v in value.items():
            _render(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list):
        if not value:
            out.append(f"{path}: []")
        for i, v in enumerate(value):
            _render(v, f"{path}[{i}]", out)
    elif isinstance(value, str) and leaf in _SIGNATURE_KEYS and len(value) > 24:
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        out.append(f"{path}: {value[:16]}... ({len(value)} chars, sha256 {digest})")
    elif isinstance(value, str) and "\n" in value:
        out.append(f"{path}: |")
        out.extend(f"  | {line}" for line in value.split("\n"))
    else:
        out.append(f"{path}: {json.dumps(value, ensure_ascii=False)}")


def render_request(canonical: Mapping[str, Any]) -> list[str]:
    """One line per leaf, multi-line strings split, signatures shortened."""
    out: list[str] = []
    _render(canonical, "", out)
    return out


def _common_prefix(a: Sequence[Any], b: Sequence[Any]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def miss_report(path: Path, canonical: Mapping[str, Any], entries: Sequence[CassetteEntry]) -> str:
    key = request_sha256(canonical)
    messages = list(canonical.get("messages") or [])
    head = f"cassette miss in {path}: no recording for request sha256 {key[:16]}"
    if not entries:
        return f"{head}; the cassette has no recordings"
    shown = scrub(canonical)

    def closeness(e: CassetteEntry) -> tuple[int, int, int, int, int]:
        recorded = list(e.request.get("messages") or [])
        prefix = _common_prefix(recorded, list(shown.get("messages") or []))
        # Longest shared prefix first, then the recording closest in length, so a
        # request is compared with the same step of the loop rather than a later one.
        length_gap = -abs(len(recorded) - len(messages))
        same_system = int(e.request.get("system") == shown.get("system"))
        same_tools = int(e.request.get("tools") == shown.get("tools"))
        return (prefix, length_gap, same_system, same_tools, e.index)

    nearest = max(entries, key=closeness)
    prefix, _, same_system, same_tools, _ = closeness(nearest)
    notes = [f"first {prefix} of {len(messages)} messages identical"]
    if not same_system:
        notes.append("system differs")
    if not same_tools:
        notes.append("tools differ")
    diff = difflib.unified_diff(
        render_request(nearest.request),
        render_request(shown),
        fromfile=f"recorded #{nearest.index} (sha256 {nearest.key[:16]})",
        tofile="this request",
        n=2,
        lineterm="",
    )
    return (
        f"{head}\nnearest recording: #{nearest.index} of {len(entries)}, "
        + ", ".join(notes)
        + "\n"
        + "\n".join(diff)
    )


# ------------------------------------------------------------------------ recording


class _CassetteWriter:
    def __init__(self, path: Path, *, fresh: bool) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            path.write_text("", encoding="utf-8")

    def append(self, canonical: Mapping[str, Any], message: AIMessage) -> None:
        line = json.dumps(
            {
                "v": CASSETTE_VERSION,
                "key": request_sha256(canonical),
                "request": scrub(canonical),
                "response": scrub(message_to_dict(message)),
            },
            # ASCII only: the decoded data is identical, and a model that writes U+2014
            # cannot put the character into a committed file (BUG-M7-2).
            ensure_ascii=True,
            separators=(",", ":"),
        )
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class CassetteRecorder(BaseChatModel):
    """Passes requests to `inner` and records each completed exchange."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseChatModel
    path: Path
    canonicalizer: Canonicalizer
    fresh: bool = False

    _writer: _CassetteWriter | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "cassette-recorder"

    @property
    def writer(self) -> _CassetteWriter:
        if self._writer is None:
            self._writer = _CassetteWriter(self.path, fresh=self.fresh)
        return self._writer

    def bind_tools(
        self,
        tools: Sequence[Mapping[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: str | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        check_tool_choice(tool_choice)
        # Let the wrapped model format its own tools, so the kwargs are exactly its own.
        bound = self.inner.bind_tools(tools, tool_choice=tool_choice, **kwargs)  # type: ignore[arg-type]
        return self.bind(**bound.kwargs)  # type: ignore[attr-defined]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        check_tool_choice(kwargs.get("tool_choice"))
        canonical = self.canonicalizer(messages, kwargs)
        message = self.inner.invoke(messages, stop=stop, **kwargs)
        if not isinstance(message, AIMessage):
            raise TypeError(f"inner model returned {type(message).__name__}")
        self.writer.append(canonical, message)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        check_tool_choice(kwargs.get("tool_choice"))
        canonical = self.canonicalizer(messages, kwargs)
        message = await self.inner.ainvoke(messages, stop=stop, **kwargs)
        if not isinstance(message, AIMessage):
            raise TypeError(f"inner model returned {type(message).__name__}")
        self.writer.append(canonical, message)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        check_tool_choice(kwargs.get("tool_choice"))
        canonical = self.canonicalizer(messages, kwargs)
        chunks: list[AIMessageChunk] = []
        async for chunk in self.inner.astream(messages, stop=stop, **kwargs):
            chunks.append(chunk)
            yield ChatGenerationChunk(message=chunk)
        # Only a completed stream is recorded; a failed call leaves no entry.
        self.writer.append(canonical, aggregate(chunks))


# -------------------------------------------------------------------------- replay


class CassetteReplayModel(BaseChatModel):
    """Serves recorded responses; never calls anything upstream."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    canonicalizer: Canonicalizer

    _entries: list[CassetteEntry] | None = PrivateAttr(default=None)
    _served: dict[str, int] = PrivateAttr(default_factory=lambda: defaultdict(int))

    @property
    def _llm_type(self) -> str:
        return "cassette-replay"

    @property
    def entries(self) -> list[CassetteEntry]:
        if self._entries is None:
            self._entries = load_cassette(self.path)
        return self._entries

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
            bound["tool_choice"] = {"type": "auto"}
        return self.bind(**bound, **kwargs)

    def lookup(self, messages: Sequence[BaseMessage], kwargs: Mapping[str, Any]) -> AIMessage:
        check_tool_choice(kwargs.get("tool_choice"))
        canonical = self.canonicalizer(messages, kwargs)
        key = request_sha256(canonical)
        matches = [e for e in self.entries if e.key == key]
        if not matches:
            raise CassetteMiss(miss_report(self.path, canonical, self.entries))
        # The same request recorded twice (a retry, say) replays in recorded order.
        n = self._served[key]
        self._served[key] = n + 1
        return message_from_dict(matches[min(n, len(matches) - 1)].response)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.lookup(messages, kwargs))])

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
        for chunk in stream_chunks(self.lookup(messages, kwargs)):
            yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in stream_chunks(self.lookup(messages, kwargs)):
            yield ChatGenerationChunk(message=chunk)
