"""Edge cases: new input while interrupted; two interrupts in one node; resume by interrupt id."""
import sqlite3
from typing import TypedDict, Annotated
import operator
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

calls = []
class S(TypedDict, total=False):
    log: Annotated[list, operator.add]
    a: str
    b: str

def ask(s):
    calls.append("ask-enter")
    a = interrupt("first?")
    calls.append(f"after-first a={a}")
    b = interrupt("second?")
    calls.append(f"after-second b={b}")
    return {"a": a, "b": b, "log": ["ask done"]}

def start(s):
    calls.append("start"); return {"log": ["start"]}

g = StateGraph(S); g.add_node("start", start); g.add_node("ask", ask)
g.add_edge(START, "start"); g.add_edge("start", "ask"); g.add_edge("ask", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
cfg = {"configurable": {"thread_id": "x"}}

print("--- two interrupts in one node")
r = app.invoke({"log": []}, cfg); print("1:", r.get("__interrupt__")[0].value, calls)
r = app.invoke(Command(resume="A"), cfg); print("2:", r.get("__interrupt__")[0].value, calls)
r = app.invoke(Command(resume="B"), cfg); print("3:", {k: v for k, v in r.items()}, calls)

print("--- new plain input while an interrupt is pending")
calls.clear(); cfg2 = {"configurable": {"thread_id": "y"}}
r = app.invoke({"log": []}, cfg2); print("pending:", app.get_state(cfg2).next)
r = app.invoke({"log": ["user said something else"]}, cfg2)
st = app.get_state(cfg2)
print("after plain input -> next:", st.next, "| log:", st.values.get("log"), "| calls:", calls)

print("--- resume keyed by interrupt id")
calls.clear(); cfg3 = {"configurable": {"thread_id": "z"}}
r = app.invoke({"log": []}, cfg3); iid = r["__interrupt__"][0].id
r = app.invoke(Command(resume={iid: "A-by-id"}), cfg3); print("by id ->", r.get("__interrupt__"), calls)

print("--- update_state while interrupted (out-of-band edit, e.g. price changed)")
cfg4 = {"configurable": {"thread_id": "w"}}
app.invoke({"log": []}, cfg4)
app.update_state(cfg4, {"log": ["price changed by app"]})
st = app.get_state(cfg4); print("after update_state next:", st.next, "log:", st.values["log"])
