"""Non-crash exception inside node; RetryPolicy; parallel branch pending writes."""
import sqlite3, operator
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import RetryPolicy
calls = []
class S(TypedDict, total=False):
    log: Annotated[list, operator.add]
fail = {"b": True, "flaky": 2}
def a(s): calls.append("a"); return {"log": ["a"]}
def b(s):
    calls.append("b")
    if fail["b"]: raise RuntimeError("tool 503")
    return {"log": ["b"]}
def flaky(s):
    calls.append("flaky")
    if fail["flaky"] > 0: fail["flaky"] -= 1; raise ConnectionError("transient")
    return {"log": ["flaky ok"]}
g = StateGraph(S)
g.add_node("a", a); g.add_node("b", b)
g.add_node("flaky", flaky, retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.01, retry_on=ConnectionError))
g.add_edge(START, "a"); g.add_edge(START, "b"); g.add_edge("a", "flaky"); g.add_edge("b", "flaky"); g.add_edge("flaky", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
cfg = {"configurable": {"thread_id": "e"}}
try: app.invoke({"log": []}, cfg, durability="sync")
except Exception as e: print("raised:", type(e).__name__, e)
st = app.get_state(cfg)
print("after error: next=", st.next, "tasks=", [(t.name, t.error) for t in st.tasks], "calls=", calls)
fail["b"] = False; calls.clear()
print("resume:", app.invoke(None, cfg, durability="sync"), "calls=", calls)
