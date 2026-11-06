"""Trace sinks. JsonlSink is always first; the others are optional views."""

from __future__ import annotations

import logging

from errand.config import LangfuseKeys, TraceSettings
from errand.trace.sinks.base import Sink
from errand.trace.sinks.null import NullSink

log = logging.getLogger(__name__)


def make_sink(settings: TraceSettings | None = None, keys: LangfuseKeys | None = None) -> Sink:
    """The optional dashboard sink. Never imports langfuse (spike F1).

    It returns NullSink unless both Langfuse keys are set and ERRAND_LANGFUSE is not
    off. The Langfuse sink itself lands in M15; until then a configured Langfuse is
    reported and ignored rather than failing the run.
    """
    settings = settings or TraceSettings()
    if settings.langfuse.lower() == "off":
        return NullSink()
    keys = keys or LangfuseKeys()
    if keys.public_key is None or keys.secret_key is None:
        return NullSink()
    log.warning("Langfuse keys are set, but the Langfuse sink lands in M15; using NullSink")
    return NullSink()
