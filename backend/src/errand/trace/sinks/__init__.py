"""Trace sinks. JsonlSink is always first; the others are optional views."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from errand.config import LangfuseKeys, TraceSettings
from errand.trace.sinks.base import ProcessSink, Sink
from errand.trace.sinks.null import NullSink

if TYPE_CHECKING:
    from errand.trace.sinks.langfuse import ClientFactory, SpanExporterLike

__all__ = ["NullSink", "ProcessSink", "Sink", "make_sink", "off_reason"]

log = logging.getLogger(__name__)

OFF_VALUES = frozenset({"off", "0", "false"})


def off_reason(settings: TraceSettings, keys: LangfuseKeys) -> str | None:
    """Why the Langfuse sink is off ("disabled" or "no_keys"), or None if it should run."""
    if settings.langfuse.strip().lower() in OFF_VALUES:
        return "disabled"
    if keys.public_key is None or keys.secret_key is None:
        return "no_keys"
    return None


def make_sink(
    settings: TraceSettings | None = None,
    keys: LangfuseKeys | None = None,
    *,
    span_exporter: SpanExporterLike | None = None,
    client_factory: ClientFactory | None = None,
) -> ProcessSink:
    """The optional dashboard sink, built once per process (the API lifespan).

    NullSink unless both Langfuse keys are set and ERRAND_LANGFUSE is not off. In that
    case langfuse is never imported (spike F1): the import below is the only path to it,
    and import-linter keeps it that way. `span_exporter` and `client_factory` are test
    seams; production passes neither.
    """
    settings = settings or TraceSettings()
    keys = keys or LangfuseKeys()
    reason = off_reason(settings, keys)
    if reason is not None:
        return NullSink(reason)
    try:
        from errand.trace.sinks.langfuse import LangfuseSink

        return LangfuseSink(keys, span_exporter=span_exporter, client_factory=client_factory)
    except Exception:
        # A dashboard that cannot start must not stop the agent: JSONL is the record.
        log.exception("the Langfuse sink could not start; traces go to JSONL only")
        return NullSink("init_failed")
