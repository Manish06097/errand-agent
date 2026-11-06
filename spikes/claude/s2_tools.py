import json, time, warnings
from typing import Literal
from pydantic import BaseModel, Field
from common import dump, msg_summary
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool

warnings.simplefilter("always")
results = {}

@tool
def search_providers(service: str, area: str, date_range: str) -> str:
    """Search for home-service providers. date_range like '2026-11-09..2026-11-13'."""
    return json.dumps([{"id": "P1", "name": "Tap Masters", "price": 1200, "rating": 4.6, "slots": ["2026-11-10T09:00"]},
                       {"id": "P2", "name": "FlowFix", "price": 1700, "rating": 4.8, "slots": ["2026-11-11T08:00"]}])

base = ChatAnthropic(model="claude-opus-5-5", max_tokens=1500, thinking={"type": "adaptive"}, output_config={"effort": "low"})
llm = base.bind_tools([search_providers], tool_choice="auto", strict=True)
print("bound kwargs:", {k: v for k, v in llm.kwargs.items() if k != "tools"}, "strict in tool:", llm.kwargs["tools"][0].get("strict"))

sysmsg = SystemMessage("You help arrange home-service errands. Use tools to look things up. Today is 2026-11-06 (Friday).")
msgs = [sysmsg, HumanMessage("Find a plumber in Indiranagar for my kitchen tap next Mon-Fri, mornings. Use search_providers.")]
t = time.time(); r1 = llm.invoke(msgs); results["turn1_call1_latency"] = round(time.time() - t, 2)
print("R1 content types:", [b.get("type") if isinstance(b, dict) else "str" for b in (r1.content if isinstance(r1.content, list) else [r1.content])])
print("R1 tool_calls:", r1.tool_calls)
results["r1"] = msg_summary(r1)
msgs.append(r1)
for tc in r1.tool_calls:
    msgs.append(ToolMessage(content=search_providers.invoke(tc["args"]), tool_call_id=tc["id"]))
t = time.time(); r2 = llm.invoke(msgs); results["turn1_call2_latency"] = round(time.time() - t, 2)
print("R2:", r2.content if isinstance(r2.content, str) else [ (b.get("type"), (b.get("text") or "")[:200]) for b in r2.content])
results["r2"] = msg_summary(r2)
dump("s2_turn1_messages.json", [msg_summary(m) if hasattr(m, "tool_calls") else {"type": type(m).__name__, "content": m.content} for m in msgs + [r2]])

# Forced tool_choice WITHOUT explicit thinking (LangChain does not drop it in this case)
forced = ChatAnthropic(model="claude-opus-5-5", max_tokens=500, effort="low").bind_tools([search_providers], tool_choice="any")
try:
    forced.invoke("Find a plumber in Indiranagar next week.")
    results["forced_any"] = "NO ERROR"
except Exception as e:
    results["forced_any"] = f"{type(e).__name__}: {str(e)[:400]}"
print("forced any:", results["forced_any"])
forced2 = ChatAnthropic(model="claude-opus-5-5", max_tokens=500, effort="low").bind_tools([search_providers], tool_choice="search_providers")
try:
    forced2.invoke("Find a plumber in Indiranagar next week.")
    results["forced_tool"] = "NO ERROR"
except Exception as e:
    results["forced_tool"] = f"{type(e).__name__}: {str(e)[:400]}"
print("forced tool:", results["forced_tool"])

# Forced with explicit thinking: LangChain warns and DROPS tool_choice (no API call needed to show)
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    b = base.bind_tools([search_providers], tool_choice="any")
    results["forced_with_thinking_kwargs"] = {k: v for k, v in b.kwargs.items() if k != "tools"}
    results["forced_with_thinking_warning"] = [str(x.message)[:200] for x in w]
print("forced+thinking:", results["forced_with_thinking_kwargs"], results["forced_with_thinking_warning"])

# Structured output
class Extract(BaseModel):
    service: Literal["plumber", "electrician", "ac_service", "other"]
    area: str | None = Field(description="Neighbourhood, null if not given")
    budget_inr: int | None
    mornings_only: bool

q = "Find a plumber for my kitchen tap this week, mornings only, under Rs 1,500."
for method in ["function_calling", "json_schema"]:
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        so = ChatAnthropic(model="claude-opus-5-5", max_tokens=1000, effort="low").with_structured_output(Extract, method=method, include_raw=True)
        try:
            t = time.time(); out = so.invoke(q)
            results[f"so_{method}"] = {"parsed": out["parsed"].model_dump() if out["parsed"] else None, "parsing_error": str(out["parsing_error"])[:300],
                                       "latency": round(time.time() - t, 2), "usage": out["raw"].usage_metadata,
                                       "raw_content": out["raw"].content, "raw_tool_calls": out["raw"].tool_calls,
                                       "warnings": [str(x.message)[:200] for x in w]}
        except Exception as e:
            results[f"so_{method}"] = f"{type(e).__name__}: {str(e)[:400]}"
    print(method, "->", results[f"so_{method}"])
dump("s2_results.json", results)
