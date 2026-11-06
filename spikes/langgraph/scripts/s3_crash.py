"""Spike 3: hard crash (os._exit) inside a node after an external side effect.
Usage: python s3_crash.py run|restart|inspect <db> <ledger> <durability>
Graph: plan -> hold -> book -> confirm. 'book' writes to an external ledger (the side effect)
then os._exit(1) before returning, but only on the first attempt (CRASH env var).
"""
import json, os, sqlite3, sys
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

mode, DB, LEDGER, DUR = sys.argv[1:5]

def ledger_append(ev):
    with open(LEDGER, "a") as f: f.write(json.dumps(ev) + "\n"); f.flush(); os.fsync(f.fileno())

class S(TypedDict, total=False):
    plan: str
    hold_id: str
    booking_id: str
    confirmed: bool

def plan(s):   ledger_append({"node": "plan", "pid": os.getpid()}); return {"plan": "P1@Tue09"}
def hold(s):   ledger_append({"node": "hold", "pid": os.getpid()}); return {"hold_id": "H1"}
def book(s):
    ledger_append({"node": "book", "side_effect": "BOOKED", "hold": s["hold_id"], "pid": os.getpid()})
    if os.environ.get("CRASH") == "1":
        print("  [book] side effect done, crashing with os._exit(1) before return", flush=True)
        os._exit(1)
    return {"booking_id": "B1"}
def confirm(s): ledger_append({"node": "confirm", "pid": os.getpid()}); return {"confirmed": True}

g = StateGraph(S)
for n, f in [("plan", plan), ("hold", hold), ("book", book), ("confirm", confirm)]: g.add_node(n, f)
g.add_edge(START, "plan"); g.add_edge("plan", "hold"); g.add_edge("hold", "book"); g.add_edge("book", "confirm"); g.add_edge("confirm", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(DB, check_same_thread=False)))
cfg = {"configurable": {"thread_id": "t"}}

def show():
    st = app.get_state(cfg)
    print(f"  get_state: next={st.next} values={st.values} step={st.metadata.get('step') if st.metadata else None}")
    print("  history (newest first):", [(h.metadata.get("step"), h.metadata.get("source"), h.next) for h in app.get_state_history(cfg)])

if mode == "run":
    app.invoke({}, cfg, durability=DUR)
elif mode == "inspect":
    show()
elif mode == "restart":
    show()
    print("  -> invoke(None, cfg) to resume from last checkpoint")
    out = app.invoke(None, cfg, durability=DUR)
    print("  final:", out)
