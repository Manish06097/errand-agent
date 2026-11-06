"""Preserved-thinking experiments.
A: full tool loop with prefix_mismatch_behavior=error (proves LangChain round-trips blocks byte-faithfully within a turn)
B: next turn rebuilt from our own state as plain text, new system prompt (no thinking replay) -> expect OK
C: replay thinking-bearing AIMessages verbatim + new user turn, unchanged prefix -> expect OK
D: replay + edit earlier user message, with 'error' -> expect 400
E: same edit, no beta header and no field -> tells whether this account is enforced by default
F: same edit with 'drop_block' -> expect OK; inspect response_metadata for input_transformations
G: replay + rebuilt (changed) system prompt with 'error' -> expect 400
"""
import copy, json, time
from common import dump, msg_summary
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage, AIMessage
from langchain_core.tools import tool

BETA = "thinking-binding-controls-2026-08-01"
R = {}

@tool
def search_providers(service: str, area: str, date_range: str) -> str:
    """Search for home-service providers. date_range like '2026-11-09..2026-11-13'."""
    return json.dumps([{"id": "P1", "name": "Tap Masters", "price": 1200, "rating": 4.6, "slots": ["2026-11-10T09:00", "2026-11-12T14:00"]},
                       {"id": "P2", "name": "FlowFix", "price": 1450, "rating": 4.8, "slots": ["2026-11-11T08:00"]},
                       {"id": "P3", "name": "AquaPro", "price": 1650, "rating": 4.9, "slots": ["2026-11-10T07:30"]}])

@tool
def get_provider(provider_id: str) -> str:
    """Get provider details and reviews."""
    return json.dumps({"id": provider_id, "years": 7, "reviews_summary": {"sentiment": "positive", "themes": ["punctual", "clean work"]}})

TOOLS = {"search_providers": search_providers, "get_provider": get_provider}

def make(mode=None, betas=True):
    thinking = {"type": "adaptive", "display": "summarized"}
    if mode:
        thinking["block_binding"] = {"prefix_mismatch_behavior": mode}
    kw = dict(model="claude-opus-5-5", max_tokens=2500, thinking=thinking, output_config={"effort": "medium"})
    if betas:
        kw["betas"] = [BETA]
    return ChatAnthropic(**kw).bind_tools(list(TOOLS.values()), tool_choice="auto")

def blocks(m):
    return [b.get("type") for b in m.content] if isinstance(m.content, list) else ["str"]

def run(llm, msgs, label):
    t = time.time()
    try:
        r = llm.invoke(msgs)
        R[label] = {"ok": True, "latency": round(time.time() - t, 2), "blocks": blocks(r), "usage": r.usage_metadata,
                    "input_transformations": r.response_metadata.get("input_transformations"),
                    "rm_keys": sorted(r.response_metadata.keys())}
        print(label, "OK", R[label]["blocks"], r.usage_metadata["input_tokens"], "in", r.usage_metadata["output_tokens"], "out", "transforms:", R[label]["input_transformations"])
        return r
    except Exception as e:
        R[label] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}
        print(label, "ERR", R[label]["error"])
        return None

SYS1 = SystemMessage("You arrange home-service errands. Today is Friday 2026-11-06. Known prefs: area Indiranagar.")
user1 = HumanMessage("Find me a plumber for my kitchen tap next Mon-Fri, mornings only, under Rs 1,500. Search, then check details of the best option, then recommend one.")
llm_err = make("error")
msgs = [SYS1, user1]
# A: tool loop
for i in range(5):
    r = run(llm_err, msgs, f"A_call{i+1}")
    if r is None: break
    msgs.append(r)
    if not r.tool_calls: break
    for tc in r.tool_calls:
        msgs.append(ToolMessage(content=TOOLS[tc["name"]].invoke(tc["args"]), tool_call_id=tc["id"]))
turn1 = list(msgs)
final1 = turn1[-1]
dump("s3_turn1.json", [msg_summary(m) if isinstance(m, AIMessage) else {"type": type(m).__name__, "content": m.content} for m in turn1])
has_thinking = [i for i, m in enumerate(turn1) if isinstance(m, AIMessage) and "thinking" in blocks(m)]
print("AI messages with thinking blocks at idx:", has_thinking)
R["thinking_idx"] = has_thinking

def final_text(m):
    return m.content if isinstance(m.content, str) else "".join(b.get("text", "") for b in m.content if b.get("type") == "text")

user2 = HumanMessage("Actually make it Tuesday only. Which one then?")
# B: rebuilt from our own state, plain text, new system prompt with different memory, no thinking replay
SYS2 = SystemMessage("You arrange home-service errands. Today is Friday 2026-11-06. Known prefs: area Indiranagar; earliest 08:00.")
msgsB = [SYS2, HumanMessage(user1.content), AIMessage(final_text(final1)), user2]
run(llm_err, msgsB, "B_plaintext_rebuild_new_system")
# C: verbatim replay, unchanged prefix
run(llm_err, turn1 + [user2], "C_verbatim_replay")
# D: verbatim replay but earlier user msg edited
edited = list(turn1); edited[1] = HumanMessage(user1.content + " (budget flexible)")
run(llm_err, edited + [user2], "D_edit_earlier_user_error")
# E: same edit, no beta / no field
run(make(None, betas=False), edited + [user2], "E_edit_no_beta_default")
# F: drop_block
run(make("drop_block"), edited + [user2], "F_edit_drop_block")
# G: changed system prompt with verbatim replay
run(llm_err, [SYS2] + turn1[1:] + [user2], "G_changed_system_error")
dump("s3_results.json", R)
