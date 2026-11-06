"""Streaming modes, custom events, async API with AsyncSqliteSaver, fake chat model for 'messages'."""
import asyncio, operator, time
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer
from langgraph.types import interrupt, Command, StreamWriter
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

llm = GenericFakeChatModel(messages=iter([AIMessage(content="Found three plumbers near you")]))

class S(TypedDict, total=False):
    log: Annotated[list, operator.add]
    reply: str
    ok: str

async def search(s):
    w = get_stream_writer()
    w({"type": "tool_call", "tool": "search_providers", "args": {"service": "plumber"}, "t": time.time()})
    await asyncio.sleep(0.01)
    w({"type": "tool_result", "tool": "search_providers", "latency_ms": 10, "n": 3})
    return {"log": ["searched"]}

async def respond(s, writer: StreamWriter):   # alternative: writer injected as a parameter
    writer({"type": "model_call_start", "model": "fake"})
    msg = await llm.ainvoke("hi")
    writer({"type": "model_call_end", "usage": msg.usage_metadata})
    return {"reply": msg.content}

async def approve(s):
    ans = interrupt({"approve": "P1 Tue 09:00 Rs1200"})
    return {"ok": ans}

g = StateGraph(S)
g.add_node("search", search); g.add_node("respond", respond); g.add_node("approve", approve)
g.add_edge(START, "search"); g.add_edge("search", "respond"); g.add_edge("respond", "approve"); g.add_edge("approve", END)

async def main():
    async with AsyncSqliteSaver.from_conn_string("../s4.sqlite") as saver:
        app = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": f"s{time.time()}"}}
        print("=== v1 multi-mode: tuples (mode, data)")
        async for mode, data in app.astream({"log": []}, cfg, stream_mode=["updates", "custom", "messages"], durability="sync"):
            if mode == "messages":
                chunk, meta = data
                print(f"  messages: {chunk.content!r} node={meta.get('langgraph_node')}")
            else:
                print(f"  {mode}: {data}")
        print("=== v2 resume: StreamPart dicts")
        async for part in app.astream(Command(resume="yes"), cfg, stream_mode=["updates", "custom", "tasks", "checkpoints", "values"], version="v2", durability="sync"):
            d = part["data"]
            if part["type"] == "checkpoints": d = {"next": d.get("next"), "step": d["metadata"].get("step")}
            print(f"  keys={sorted(part.keys())} type={part['type']} ns={part.get('ns')} data={str(d)[:110]}")
        st = await app.aget_state(cfg)
        print("aget_state next:", st.next, "values:", st.values)
        hist = [h async for h in app.aget_state_history(cfg, limit=3)]
        print("aget_state_history(limit=3):", [(h.metadata["step"], h.next) for h in hist])
        try:
            app.get_state(cfg)
        except Exception as e:
            print("sync get_state on AsyncSqliteSaver inside loop ->", type(e).__name__, str(e)[:120])

asyncio.run(main())
