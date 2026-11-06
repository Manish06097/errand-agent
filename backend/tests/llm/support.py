"""Shared builders for the model-gateway tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from errand.config import ModelMode, ModelSettings
from errand.llm.gateway import LLMGateway
from errand.trace.reader import read_trace
from errand.trace.sinks.base import Sink
from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.tracer import Tracer
from tests.hygiene.repo import REPO_ROOT

SPIKE_OUT = REPO_ROOT / "spikes" / "claude" / "out"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# usage_metadata of the two real Opus 5.5 calls in spike s4, verbatim.
S4_USAGE: list[dict[str, Any]] = [
    rec["usage_metadata"] for rec in json.loads((SPIKE_OUT / "s4_usage_cache.json").read_text())
]

SEARCH_TOOL: dict[str, Any] = {
    "name": "search_providers",
    "description": "Search home-service providers.",
    "input_schema": {
        "type": "object",
        "properties": {"service": {"type": "string"}, "area": {"type": "string"}},
        "required": ["service", "area"],
        "additionalProperties": False,
    },
}

SYSTEM = "You arrange home-service errands.\n" + "\n".join(
    f"Rule {i}: never book without an approval that matches provider, slot and price."
    for i in range(6)
)


def tool_use(
    usage: dict[str, Any], *, call_id: str = "toolu_01", signature: str = "sig-" + "A" * 900
) -> AIMessage:
    """A thinking-bearing tool call shaped like the spike's real responses (s3_turn1.json)."""
    args = {"service": "plumber", "area": "Indiranagar"}
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": "Search first, then compare.", "signature": signature},
            {"type": "tool_use", "id": call_id, "name": "search_providers", "input": args},
        ],
        tool_calls=[{"name": "search_providers", "args": args, "id": call_id, "type": "tool_call"}],
        usage_metadata=usage,
        response_metadata={"stop_reason": "tool_use", "model_name": "claude-opus-5-5"},
    )


def text(usage: dict[str, Any], body: str = "Tap Masters can come Tuesday at 09:00.") -> AIMessage:
    return AIMessage(
        content=[{"type": "text", "text": body}],
        usage_metadata=usage,
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5"},
    )


@dataclass
class Rig:
    gateway: LLMGateway
    tracer: Tracer
    trace_path: Path

    def records(self) -> list[dict[str, Any]]:
        return read_trace(self.trace_path).records

    def of_type(self, record_type: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["type"] == record_type]


def rig(
    tmp_path: Path,
    *,
    mode: ModelMode = "scripted",
    transport: BaseChatModel | None = None,
    tools: Sequence[Any] = (SEARCH_TOOL,),
    cassette: Path | None = None,
    run_id: str = "run-0001",
    extra_sinks: Sequence[Sink] = (),
    model_settings: Mapping[str, Any] | None = None,
    perf: Callable[[], float] | None = None,
    **gateway_kwargs: Any,
) -> Rig:
    sink = JsonlSink.for_run(tmp_path / "traces", "c_01", run_id)
    tracer_kwargs: dict[str, Any] = {} if perf is None else {"perf": perf}
    tracer = Tracer(run_id=run_id, thread_id="c_01", sinks=[sink, *extra_sinks], **tracer_kwargs)
    gateway = LLMGateway(
        tracer=tracer,
        tools=tools,
        settings=ModelSettings(model_mode=mode, **(model_settings or {})),
        transport=transport,
        cassette_path=cassette,
        **gateway_kwargs,
    )
    return Rig(gateway, tracer, sink.path)
