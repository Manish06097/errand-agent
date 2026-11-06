import sqlite3, operator
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command, StateSnapshot
class S(TypedDict, total=False):
    log: Annotated[list, operator.add]
    phase: str
def a(s): return {"log": ["a"], "phase": "searching"}
def b(s): return {"log": [f"b:{interrupt('approve?')}"], "phase": "approved"}
g = StateGraph(S); g.add_node("a", a); g.add_node("b", b)
g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", END)
saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
app = g.compile(checkpointer=saver)
cfg = {"configurable": {"thread_id": "i"}}
app.invoke({"log": []}, cfg, durability="sync")
st = app.get_state(cfg)
print("StateSnapshot fields:", StateSnapshot._fields)
print("values:", st.values); print("next:", st.next); print("metadata:", st.metadata); print("created_at:", st.created_at)
print("config:", st.config); print("parent_config:", st.parent_config)
print("tasks[0]:", st.tasks[0]); print("interrupts:", st.interrupts)
app.invoke(Command(resume="yes"), cfg, durability="sync")
print("\nhistory newest-first:")
for h in app.get_state_history(cfg):
    print(f"  step={h.metadata['step']:>2} source={h.metadata['source']:<6} next={h.next} phase={h.values.get('phase')} ckpt={h.config['configurable']['checkpoint_id'][:13]}")
hs = list(app.get_state_history(cfg))
print("\nfilter={'source':'input'}:", [h.metadata["step"] for h in app.get_state_history(cfg, filter={"source": "input"})])
print("limit=2:", [h.metadata["step"] for h in app.get_state_history(cfg, limit=2)])
print("before=<step1 cfg>:", [h.metadata["step"] for h in app.get_state_history(cfg, before=hs[1].config)])
old = app.get_state(hs[2].config); print("get_state at a past checkpoint_id -> step", old.metadata["step"], "next", old.next)
print("saver.list() tuples:", [ (t.metadata["step"], list(t.checkpoint["channel_values"].keys())) for t in saver.list(cfg, limit=2)])
print("get_graph().draw_mermaid():"); print(app.get_graph().draw_mermaid())
