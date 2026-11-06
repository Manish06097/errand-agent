import json, random
import common
from common import dump
import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

client = anthropic.Anthropic()
random.seed(7)
english = ("Find a plumber for my kitchen tap this week, mornings only, under Rs 1,500. I live in Indiranagar and the tap has been "
           "leaking since Tuesday. Last time the electrician came late, so please pick someone with good punctuality reviews. "
           "Also, move my AC servicing to Saturday if possible, but not before 10 a.m. I prefer the same technician as last time. ") * 4
providers = [{"id": f"P{i}", "name": f"Provider {i}", "price": random.randint(800, 2200), "rating": round(random.uniform(3.5, 5), 1),
              "slots": [f"2026-11-{random.randint(9,14):02d}T{random.randint(7,17):02d}:00" for _ in range(3)],
              "attributes": {"verified": True, "years": random.randint(1, 15), "languages": ["en", "kn"]}} for i in range(8)]
json_text = json.dumps(providers)
json_pretty = json.dumps(providers, indent=2)
memories = "\n".join(f"- [explicit] pref_{i}: earliest_time=0{7 + i % 3}:00 area=Indiranagar technician=T{i}" for i in range(50))
samples = {"english": english, "json_compact": json_text, "json_pretty": json_pretty, "memory_lines": memories,
           "hindi_mixed": "Mujhe kal subah plumber chahiye, budget ₹1500 tak. नल टपक रहा है। " * 6}
base = client.messages.count_tokens(model="claude-opus-5-5", messages=[{"role": "user", "content": "x"}]).input_tokens
res = {"overhead_single_user_msg_x": base}
for model in ["claude-opus-5-5", "claude-haiku-5-5"]:
    for k, v in samples.items():
        n = client.messages.count_tokens(model=model, messages=[{"role": "user", "content": v}]).input_tokens
        res[f"{model}:{k}"] = {"chars": len(v), "tokens": n, "chars_per_token": round(len(v) / n, 2),
                               "chars_per_token_minus_overhead": round(len(v) / max(1, n - base), 2)}
        print(model, k, res[f"{model}:{k}"])
# tools overhead and system
tool = {"name": "search_providers", "description": "Search providers", "input_schema": {"type": "object", "properties": {
    "service": {"type": "string"}, "area": {"type": "string"}, "date_range": {"type": "string"}}, "required": ["service", "area", "date_range"]}}
with_tool = client.messages.count_tokens(model="claude-opus-5-5", tools=[tool], messages=[{"role": "user", "content": "x"}]).input_tokens
res["one_tool_overhead"] = with_tool - base
print("one tool overhead", with_tool - base)
# forced tool_choice on count_tokens
try:
    client.messages.count_tokens(model="claude-opus-5-5", tools=[tool], tool_choice={"type": "any"}, messages=[{"role": "user", "content": "x"}])
    res["count_tokens_forced"] = "ok"
except Exception as e:
    res["count_tokens_forced"] = str(e)[:200]
print("count_tokens forced:", res["count_tokens_forced"])
# LangChain wrapper
llm = ChatAnthropic(model="claude-opus-5-5")
res["langchain_get_num_tokens_from_messages"] = llm.get_num_tokens_from_messages([SystemMessage("You help with errands."), HumanMessage(english)])
print("langchain count:", res["langchain_get_num_tokens_from_messages"])
dump("s5_tokens.json", res)
