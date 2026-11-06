"""Streaming: does an aggregated AIMessageChunk (thinking + signature + tool_use) round-trip under prefix_mismatch_behavior=error?"""
import time
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
import s3_thinking_lib as L
from common import dump

llm = L.make("error")
msgs = [SystemMessage("You arrange home-service errands. Today is Friday 2026-11-06. Known prefs: area Indiranagar."),
        HumanMessage("Find a plumber next Mon-Fri, mornings, under Rs 1,500. Search, check details of the best one, then recommend.")]
log = []
for i in range(4):
    t = time.time(); first = None; agg = None; n = 0; kinds = set()
    for ch in llm.stream(msgs):
        n += 1
        if first is None: first = round(time.time() - t, 2)
        if isinstance(ch.content, list):
            for b in ch.content: kinds.add(b.get("type"))
        agg = ch if agg is None else agg + ch
    sig_ok = [len(b.get("signature", "")) for b in agg.content if isinstance(b, dict) and b.get("type") == "thinking"] if isinstance(agg.content, list) else []
    rec = {"call": i + 1, "chunks": n, "ttft_s": first, "total_s": round(time.time() - t, 2), "chunk_block_types": sorted(k for k in kinds if k),
           "final_blocks": L.blocks(agg), "thinking_sig_lens": sig_ok, "tool_calls": [tc["name"] for tc in agg.tool_calls], "usage": agg.usage_metadata}
    print(rec); log.append(rec)
    msgs.append(agg)
    if not agg.tool_calls: break
    for tc in agg.tool_calls:
        msgs.append(ToolMessage(L.TOOLS[tc["name"]].invoke(tc["args"]), tool_call_id=tc["id"]))
dump("s8_stream.json", log)
