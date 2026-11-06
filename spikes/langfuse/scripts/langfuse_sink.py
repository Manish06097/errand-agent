"""Prototype: optional Langfuse sink fed by our own tracer. JSONL stays the source of truth."""
from __future__ import annotations
import os
from typing import Any, Protocol


class TraceSink(Protocol):
    def span_start(self, *, run_id: str, span_id: str, parent_id: str | None, kind: str, name: str,
                   input: Any = None, attrs: dict | None = None) -> None: ...
    def span_end(self, *, span_id: str, output: Any = None, level: str | None = None,
                 status: str | None = None, model: str | None = None,
                 usage: dict | None = None, cost_usd: dict | None = None, end_ns: int | None = None) -> None: ...
    def event(self, *, run_id: str, parent_id: str | None, name: str, attrs: dict | None = None) -> None: ...
    def close(self) -> None: ...


class NullSink:
    def span_start(self, **_: Any) -> None: pass
    def span_end(self, **_: Any) -> None: pass
    def event(self, **_: Any) -> None: pass
    def close(self) -> None: pass


_KIND_TO_TYPE = {"run": "agent", "node": "chain", "model": "generation", "tool": "tool",
                 "reader": "generation", "guard": "guardrail", "span": "span"}


class LangfuseSink:
    """Emits observations live (start at span_start, end at span_end) so durations are real."""

    def __init__(self, *, session_id: str | None = None, user_id: str | None = None, **client_kwargs: Any):
        from langfuse import Langfuse, propagate_attributes  # lazy: never imported when unconfigured
        self._propagate = propagate_attributes
        from opentelemetry.sdk.trace import TracerProvider
        # Own TracerProvider: do not hijack the global OTel provider.
        self._lf = Langfuse(tracer_provider=client_kwargs.pop("tracer_provider", TracerProvider()), **client_kwargs)
        self._obs: dict[str, Any] = {}
        self._roots: dict[str, Any] = {}
        self._session_id, self._user_id = session_id, user_id

    def _parent(self, run_id: str, parent_id: str | None):
        return self._obs.get(parent_id) if parent_id else self._roots.get(run_id)

    def span_start(self, *, run_id, span_id, parent_id, kind, name, input=None, attrs=None):
        as_type = _KIND_TO_TYPE.get(kind, "span")
        parent = self._parent(run_id, parent_id)
        # v5 has no update_trace(): trace-level fields are stamped on every span started
        # inside propagate_attributes(), so wrap each start.
        with self._propagate(session_id=self._session_id, user_id=self._user_id,
                             trace_name="errand_run", metadata={"run_id": run_id}):
            if parent is None:  # root of a run: deterministic trace id from our run id
                obs = self._lf.start_observation(
                    trace_context={"trace_id": self._lf.create_trace_id(seed=run_id)},
                    name=name, as_type=as_type, input=input, metadata=attrs)
                self._roots[run_id] = obs
            else:
                obs = parent.start_observation(name=name, as_type=as_type, input=input, metadata=attrs)
        self._obs[span_id] = obs

    def span_end(self, *, span_id, output=None, level=None, status=None, model=None,
                 usage=None, cost_usd=None, end_ns=None):
        obs = self._obs.pop(span_id, None)
        if obs is None:
            return
        upd: dict[str, Any] = {"output": output}
        if level: upd["level"] = level
        if status: upd["status_message"] = status
        if model: upd["model"] = model
        if usage: upd["usage_details"] = usage
        if cost_usd: upd["cost_details"] = cost_usd  # our numbers win over server-side inference
        obs.update(**upd)
        obs.end(end_time=end_ns)

    def event(self, *, run_id, parent_id, name, attrs=None):
        parent = self._parent(run_id, parent_id)
        if parent is not None:
            with self._propagate(session_id=self._session_id, user_id=self._user_id,
                                 trace_name="errand_run", metadata={"run_id": run_id}):
                parent.create_event(name=name, metadata=attrs)

    def close(self):
        self._lf.shutdown()  # flushes; call from app shutdown, never per request


def make_sink(**kw: Any) -> TraceSink:
    enabled = (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
               and os.getenv("ERRAND_LANGFUSE", "auto").lower() not in ("0", "false", "off"))
    return LangfuseSink(**kw) if enabled else NullSink()
