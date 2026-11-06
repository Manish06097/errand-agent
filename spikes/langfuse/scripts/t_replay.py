"""Replay a finished event with real past timestamps (end_time is public; start_time is not)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import netguard
for k in list(os.environ):
    if k.startswith("LANGFUSE_"): del os.environ[k]
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from langfuse import Langfuse
exp = InMemorySpanExporter()
lf = Langfuse(public_key="pk", secret_key="sk", base_url="http://127.0.0.1:9", tracer_provider=TracerProvider(), span_exporter=exp)
now = time.time_ns(); past_start = now - 5_000_000_000; past_end = now - 4_200_000_000
# Public API: start is 'now', end can be explicit
o = lf.start_observation(name="tool:book(public)", as_type="tool"); o.end(end_time=past_end)
lf.flush()
for s in exp.get_finished_spans():
    print(s.name, "start_offset_s=%.2f" % ((s.start_time - now)/1e9), "end_offset_s=%.2f" % ((s.end_time - now)/1e9))
