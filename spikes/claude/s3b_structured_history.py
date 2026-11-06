"""H: prior turn kept as structured tool_use/tool_result but thinking blocks stripped, new system prompt,
then continue the new turn's tool loop under prefix_mismatch_behavior=error. Proves the 'rebuild each turn,
append-only within a turn' rule end to end."""
import json
from common import dump
from langchain_core.messages import AIMessage, ToolMessage, HumanMessage, SystemMessage
import s3_thinking_lib as L

turn1 = L.load_turn1()
stripped = []
for m in turn1[1:]:
    if isinstance(m, AIMessage) and isinstance(m.content, list):
        stripped.append(AIMessage(content=[b for b in m.content if b.get("type") != "thinking"], tool_calls=m.tool_calls))
    else:
        stripped.append(m)
SYS3 = SystemMessage("You arrange home-service errands. Today is Friday 2026-11-06. Known prefs: area Indiranagar; earliest 08:00; prefers FlowFix.")
msgs = [SYS3] + stripped + [HumanMessage("Get details on P1 too, then tell me which one for Tuesday.")]
llm = L.make("error")
out = []
for i in range(4):
    r = llm.invoke(msgs)
    out.append({"blocks": L.blocks(r), "transforms": r.response_metadata.get("input_transformations"), "in": r.usage_metadata["input_tokens"], "out": r.usage_metadata["output_tokens"]})
    print("H call", i + 1, out[-1])
    msgs.append(r)
    if not r.tool_calls: break
    for tc in r.tool_calls:
        msgs.append(ToolMessage(content=L.TOOLS[tc["name"]].invoke(tc["args"]), tool_call_id=tc["id"]))
dump("s3b_results.json", out)
