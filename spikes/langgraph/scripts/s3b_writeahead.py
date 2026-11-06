"""Write-ahead intent pattern surviving os._exit inside the booking node.
Our own SQLite table `booking_intents` (same DB file, separate connection) is the durable guard.
Mock provider keeps bookings in its own file (simulates the remote side).
Usage: python s3b_writeahead.py run|restart <db> <provider_db>
"""
import json, os, sqlite3, sys, uuid
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

mode, DB, PROV = sys.argv[1:4]

# --- mock provider (remote side, its own durable store) ---
pc = sqlite3.connect(PROV, isolation_level=None)
pc.execute("create table if not exists bookings(idem text primary key, hold text, booking_id text)")
def provider_book(hold_id, idem):
    row = pc.execute("select booking_id from bookings where idem=?", (idem,)).fetchone()
    if row: return {"status": "confirmed", "booking_id": row[0], "dedup": True}
    bid = "B-" + uuid.uuid4().hex[:6]
    pc.execute("insert into bookings values(?,?,?)", (idem, hold_id, bid))
    return {"status": "confirmed", "booking_id": bid}
def provider_status(idem):
    row = pc.execute("select booking_id from bookings where idem=?", (idem,)).fetchone()
    return {"status": "confirmed", "booking_id": row[0]} if row else {"status": "not_found"}

# --- app-owned write-ahead table, same file as checkpoints ---
ac = sqlite3.connect(DB, isolation_level=None, check_same_thread=False)
ac.execute("pragma journal_mode=wal")
ac.execute("create table if not exists booking_intents(thread_id text, hold_id text, idem text primary key, state text, booking_id text)")

class S(TypedDict, total=False):
    hold_id: str
    booking_id: str
    recovered: bool

def hold(s): return {"hold_id": "H1"}

def book(s, config):
    tid = config["configurable"]["thread_id"]
    row = ac.execute("select idem, state, booking_id from booking_intents where thread_id=? and hold_id=?", (tid, s["hold_id"])).fetchone()
    if row and row[1] == "done":
        return {"booking_id": row[2]}
    if row:  # intent exists but outcome unknown -> ask source of truth FIRST
        idem = row[0]
        st = provider_status(idem)
        print(f"  [book] found in-flight intent {idem}, get_booking_status -> {st}")
        if st["status"] == "confirmed":
            ac.execute("update booking_intents set state='done', booking_id=? where idem=?", (st["booking_id"], idem))
            return {"booking_id": st["booking_id"], "recovered": True}
        # not_found -> safe to call book with the SAME key
    else:
        idem = "idem-" + uuid.uuid4().hex[:8]
        ac.execute("insert into booking_intents values(?,?,?,'pending',NULL)", (tid, s["hold_id"], idem))  # committed (autocommit)
    res = provider_book(s["hold_id"], idem)
    if os.environ.get("CRASH") == "1":
        print("  [book] provider booked, crashing before recording outcome", flush=True); os._exit(1)
    ac.execute("update booking_intents set state='done', booking_id=? where idem=?", (res["booking_id"], idem))
    return {"booking_id": res["booking_id"]}

g = StateGraph(S); g.add_node("hold", hold); g.add_node("book", book)
g.add_edge(START, "hold"); g.add_edge("hold", "book"); g.add_edge("book", END)
app = g.compile(checkpointer=SqliteSaver(sqlite3.connect(DB, check_same_thread=False)))
cfg = {"configurable": {"thread_id": "t"}}
if mode == "run": app.invoke({}, cfg, durability="sync")
else:
    print("  next before restart:", app.get_state(cfg).next)
    print("  final:", app.invoke(None, cfg, durability="sync"))
print("  provider bookings:", pc.execute("select * from bookings").fetchall())
print("  intents:", ac.execute("select * from booking_intents").fetchall())
