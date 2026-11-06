import sqlite3, sys, warnings
from datetime import datetime
from decimal import Decimal
warnings.simplefilter("always")
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt
from s6pkg.models import State, Proposal, Phase
def prop(s: State):
    return {"phase": Phase.AWAITING_APPROVAL, "proposal": Proposal(provider_id="P1", slot=datetime(2026,11,10,9), price=Decimal("1200.50"))}
def appr(s: State):
    interrupt("ok?"); return {}
g = StateGraph(State); g.add_node("prop", prop); g.add_node("appr", appr)
g.add_edge(START, "prop"); g.add_edge("prop", "appr"); g.add_edge("appr", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(sys.argv[2], check_same_thread=False)))
cfg = {"configurable": {"thread_id": "p"}}
if sys.argv[1] == "write":
    out = app.invoke(State(), cfg, durability="sync"); print("invoke returns type:", type(out).__name__)
else:
    v = app.get_state(cfg).values
    print("values type:", type(v).__name__, "| proposal type:", type(v["proposal"]).__name__, "| phase:", repr(v["phase"]), "| price:", repr(v["proposal"].price), "| slot:", repr(v["proposal"].slot))
