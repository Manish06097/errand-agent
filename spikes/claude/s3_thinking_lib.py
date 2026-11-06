import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
import common  # loads key

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

def make(mode=None):
    thinking = {"type": "adaptive", "display": "summarized"}
    if mode:
        thinking["block_binding"] = {"prefix_mismatch_behavior": mode}
    return ChatAnthropic(model="claude-opus-5-5", max_tokens=2500, thinking=thinking, output_config={"effort": "medium"},
                         betas=["thinking-binding-controls-2026-08-01"]).bind_tools(list(TOOLS.values()), tool_choice="auto")

def blocks(m):
    return [b.get("type") for b in m.content] if isinstance(m.content, list) else ["str"]

def load_turn1():
    raw = json.load(open(common.OUT + "/s3_turn1.json"))
    msgs, pending = [], []
    for m in raw:
        if m["type"] == "SystemMessage": msgs.append(SystemMessage(m["content"]))
        elif m["type"] == "HumanMessage": msgs.append(HumanMessage(m["content"]))
        elif m["type"] == "ToolMessage": msgs.append(ToolMessage(m["content"], tool_call_id=pending.pop(0)))
        else:
            msgs.append(AIMessage(content=m["content"], tool_calls=m["tool_calls"] or []))
            pending = [tc["id"] for tc in (m["tool_calls"] or [])]
    return msgs
