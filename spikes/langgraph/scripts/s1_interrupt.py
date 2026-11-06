"""Spike 1+2: interrupt()/Command(resume) across separate processes, and node re-execution on resume.
Usage: python s1_interrupt.py start|resume|inspect <db> <counter_file>
"""
import json, os, sqlite3, sys
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

mode, DB, COUNTER = sys.argv[1], sys.argv[2], sys.argv[3]

def bump(key):
    d = json.load(open(COUNTER)) if os.path.exists(COUNTER) else {}
    d[key] = d.get(key, 0) + 1
    json.dump(d, open(COUNTER, "w"))
    return d[key]

class S(TypedDict, total=False):
    proposal: str
    decision: str
    booked: bool

def propose(s: S):
    bump("propose")
    return {"proposal": "book P1 @ Tue 09:00 for 1200"}

def approve(s: S):
    n = bump("approve_before_interrupt")
    print(f"  [approve] entered node, pre-interrupt counter={n}, pid={os.getpid()}")
    answer = interrupt({"ask": "approve?", "proposal": s["proposal"]})
    bump("approve_after_interrupt")
    print(f"  [approve] got resume value={answer!r}")
    return {"decision": answer}

def book(s: S):
    bump("book")
    return {"booked": s["decision"] == "yes"}

g = StateGraph(S)
g.add_node("propose", propose); g.add_node("approve", approve); g.add_node("book", book)
g.add_edge(START, "propose"); g.add_edge("propose", "approve"); g.add_edge("approve", "book"); g.add_edge("book", END)

conn = sqlite3.connect(DB, check_same_thread=False)
app = g.compile(checkpointer=SqliteSaver(conn))
cfg = {"configurable": {"thread_id": "t1"}}

if mode == "start":
    out = app.invoke({}, cfg)
    print("  invoke returned:", out)
elif mode == "start_v2":
    out = app.invoke({}, cfg, version="v2")
    print("  invoke(v2) returned type:", type(out).__name__, "->", out)
elif mode == "resume":
    out = app.invoke(Command(resume="yes"), cfg)
    print("  invoke returned:", out)
elif mode == "inspect":
    st = app.get_state(cfg)
    print("  next:", st.next)
    print("  values:", st.values)
    print("  interrupts:", st.interrupts)
    print("  tasks:", [(t.name, t.interrupts) for t in st.tasks])
print("  counters:", json.load(open(COUNTER)))
