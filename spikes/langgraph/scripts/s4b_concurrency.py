import asyncio, operator, time
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt, Command
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

side_effects = []
class S(TypedDict, total=False):
    ok: str
    note: str
async def reader(s):
    # quarantined reader output should not stream to the UI
    m = GenericFakeChatModel(messages=iter([AIMessage(content="structured review fields")])).with_config(tags=["nostream"])
    r = await m.ainvoke("x")
    return {"note": r.content}
async def approve(s):
    return {"ok": interrupt("approve?")}
async def book(s, config):
    side_effects.append(config["configurable"]["thread_id"])
    await asyncio.sleep(0.05)
    return {}
g = StateGraph(S)
for n, f in [("reader", reader), ("approve", approve), ("book", book)]: g.add_node(n, f)
g.add_edge(START, "reader"); g.add_edge("reader", "approve"); g.add_edge("approve", "book"); g.add_edge("book", END)

async def main():
    async with AsyncSqliteSaver.from_conn_string("../s4b.sqlite") as saver:
        app = g.compile(checkpointer=saver)
        # nostream check
        cfg = {"configurable": {"thread_id": f"n{time.time()}"}}
        msgs = [d async for m, d in app.astream({}, cfg, stream_mode=["messages"])]
        print("messages chunks from nostream-tagged model:", len(msgs))
        # 30 concurrent threads, one shared saver
        t0 = time.time()
        cfgs = [{"configurable": {"thread_id": f"c{i}-{t0}"}} for i in range(30)]
        await asyncio.gather(*[app.ainvoke({}, c) for c in cfgs])
        await asyncio.gather(*[app.ainvoke(Command(resume="yes"), c) for c in cfgs])
        states = [await app.aget_state(c) for c in cfgs]
        print(f"30 concurrent threads: book ran {len(side_effects)} times, {time.time()-t0:.2f}s, all done:", all(s.next == () for s in states))
        # double-click approve: two concurrent resumes on the SAME thread
        side_effects.clear()
        c = {"configurable": {"thread_id": f"dbl{t0}"}}
        await app.ainvoke({}, c)
        r = await asyncio.gather(app.ainvoke(Command(resume="yes"), c), app.ainvoke(Command(resume="yes"), c), return_exceptions=True)
        print("double resume same thread: book side effect ran", len(side_effects), "times; results:", [type(x).__name__ for x in r])
        # with an app-level per-thread lock
        side_effects.clear(); locks = {}
        async def resume_locked(c, v):
            lk = locks.setdefault(c["configurable"]["thread_id"], asyncio.Lock())
            async with lk:
                st = await app.aget_state(c)
                if not st.interrupts: return "no pending interrupt, ignored"
                return await app.ainvoke(Command(resume=v), c)
        c = {"configurable": {"thread_id": f"dbl2{t0}"}}
        await app.ainvoke({}, c)
        r = await asyncio.gather(resume_locked(c, "yes"), resume_locked(c, "yes"))
        print("with per-thread lock + pending-interrupt check: book ran", len(side_effects), "times;", [str(x)[:40] for x in r])
asyncio.run(main())
