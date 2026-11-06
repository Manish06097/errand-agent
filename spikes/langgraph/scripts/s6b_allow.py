import sqlite3, sys, warnings
warnings.simplefilter("always")
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from s6pkg.models import State
serde = JsonPlusSerializer(allowed_msgpack_modules=[("s6pkg.models", "Phase"), ("s6pkg.models", "Proposal")])
g = StateGraph(State); g.add_node("n", lambda s: {}); g.add_edge(START, "n"); g.add_edge("n", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(sys.argv[1], check_same_thread=False), serde=serde))
v = app.get_state({"configurable": {"thread_id": "p"}}).values
print("with allowlist:", type(v["proposal"]).__name__, repr(v["phase"]))
