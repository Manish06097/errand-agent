import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import netguard
for k in list(os.environ):
    if k.startswith("LANGFUSE_"): del os.environ[k]
import langfuse_sink as ls
s = ls.make_sink()
print("unconfigured ->", type(s).__name__, "| langfuse imported:", "langfuse" in sys.modules)

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
exp = InMemorySpanExporter()
s = ls.LangfuseSink(session_id="conv-42", user_id="u-7", public_key="pk", secret_key="sk",
                    base_url="http://127.0.0.1:9", span_exporter=exp, tracer_provider=TracerProvider())
s.span_start(run_id="run-1", span_id="a", parent_id=None, kind="run", name="errand_turn", input={"user": "find plumber"})
s.span_start(run_id="run-1", span_id="b", parent_id="a", kind="model", name="planner")
s.span_end(span_id="b", model="claude-opus-5-5", usage={"input": 900, "output": 120},
           cost_usd={"input": 0.0036, "output": 0.0024, "total": 0.006})
s.span_start(run_id="run-1", span_id="c", parent_id="a", kind="tool", name="book", input={"hold_id": "h1", "idempotency_key": "k1"})
s.span_end(span_id="c", output={"error": "timeout"}, level="WARNING", status="timeout")
s.event(run_id="run-1", parent_id="a", name="state:BOOKING->VERIFYING", attrs={"reason": "timeout"})
s.span_end(span_id="a", output={"outcome": "booked_verified"})
s.close()
for sp in exp.get_finished_spans():
    a = sp.attributes
    print(f"{sp.name:28s} type={a.get('langfuse.observation.type'):10s} trace={format(sp.context.trace_id,'032x')[:8]} "
          f"session={a.get('session.id')} tname={a.get('langfuse.trace.name')} cost={a.get('langfuse.observation.cost_details')}")
print("NETWORK:", netguard.ATTEMPTS)
print("LOGS:", [r for r in netguard.CAP.records if not r.startswith("DEBUG")])
