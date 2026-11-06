"""Run CallbackHandler + LangGraph + manual spans against an in-memory exporter; print what would be sent."""
import os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
import netguard
for k in list(os.environ):
    if k.startswith("LANGFUSE_"): del os.environ[k]
from typing import TypedDict
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from langfuse import Langfuse, get_client, propagate_attributes
from langfuse.langchain import CallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END

exporter = InMemorySpanExporter()
def mask(*, data, **kw):  # applied to input/output/metadata of every observation
    s = json.dumps(data, default=str)
    return json.loads(s.replace("9876543210", "[PHONE]")) if isinstance(data, (dict, list)) else data
lf = Langfuse(public_key="pk-lf-x", secret_key="sk-lf-x", base_url="http://127.0.0.1:9",
              tracer_provider=TracerProvider(), span_exporter=exporter, mask=mask)

class S(TypedDict):
    q: str
    a: str
def make_msg():
    return AIMessage(content="ok", response_metadata={"model_name": "claude-opus-5-5"},
        usage_metadata={"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
                        "input_token_details": {"cache_read": 1000, "cache_creation": 0}})
llm = GenericFakeChatModel(messages=iter([make_msg()]))
def plan(state: S): return {"a": llm.invoke(state["q"]).content}
g = StateGraph(S); g.add_node("plan", plan); g.add_edge(START, "plan"); g.add_edge("plan", END)
app = g.compile()

run_id = "run-0001"
trace_id = Langfuse.create_trace_id(seed=run_id)  # deterministic: our run id -> langfuse trace id
handler = CallbackHandler(trace_context={"trace_id": trace_id})
app.invoke({"q": "plumber, call 9876543210"}, config={
    "callbacks": [handler], "run_name": "errand_turn",
    "metadata": {"langfuse_session_id": "conv-42", "langfuse_user_id": "u-7",
                 "langfuse_tags": ["eval"], "run_id": run_id}})

# Manual observations in the same trace (what our tracer exporter would emit)
with propagate_attributes(session_id="conv-42", user_id="u-7", trace_name="errand_turn"):
    root = lf.start_observation(trace_context={"trace_id": trace_id}, name="state_transition",
                                as_type="span", input={"from": "COLLECTING", "to": "AWAITING_APPROVAL"})
    tool = root.start_observation(name="tool:book", as_type="tool", input={"hold_id": "h1", "idempotency_key": "k1"})
    tool.update(output={"error": "timeout"}, level="WARNING", status_message="timeout"); tool.end()
    gen = root.start_observation(name="planner", as_type="generation", model="claude-opus-5-5",
        usage_details={"input": 200, "input_cache_read": 1000, "output": 300},
        cost_details={"input": 0.0008, "input_cache_read": 0.0004, "output": 0.006})
    gen.end()
    root.create_event(name="approval_granted", metadata={"fingerprint": "sha256:abc"})
    root.end()
lf.flush()

for s in exporter.get_finished_spans():
    a = dict(s.attributes or {})
    keep = {k: v for k, v in a.items() if k.startswith("langfuse.") or k.startswith("user.") or k.startswith("session.")}
    print(f"--- span name={s.name!r} trace_id={format(s.context.trace_id,'032x')} parent={'yes' if s.parent else 'root'}")
    for k in sorted(keep): print(f"     {k} = {str(keep[k])[:150]}")
print("trace_id_expected", trace_id)
print("NETWORK:", netguard.ATTEMPTS)
print("LOGS:", [r for r in netguard.CAP.records if not r.startswith("DEBUG")])
