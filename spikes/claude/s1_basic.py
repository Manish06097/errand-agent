import time
from common import dump, msg_summary
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-opus-5-5", max_tokens=1024, effort="low")
# What does the payload look like?
p = llm._get_request_payload([("user", "hi")])
print("PAYLOAD keys:", {k: v for k, v in p.items() if k not in ("messages",)})

llm2 = ChatAnthropic(model="claude-opus-5-5", max_tokens=1024, output_config={"effort": "low"}, thinking={"type": "adaptive"})
p2 = llm2._get_request_payload([("user", "hi")])
print("PAYLOAD2 keys:", {k: v for k, v in p2.items() if k not in ("messages",)})

llm3 = ChatAnthropic(model="claude-opus-5-5", max_tokens=1024)
p3 = llm3._get_request_payload([("user", "hi")])
print("PAYLOAD3 (no effort):", {k: v for k, v in p3.items() if k not in ("messages",)})

t = time.time()
r = llm2.invoke("In one short sentence: what is a plumber?")
print("latency", round(time.time() - t, 2))
s = msg_summary(r); print(s)
dump("s1_basic.json", s)
