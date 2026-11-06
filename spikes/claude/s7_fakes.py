"""Offline: which langchain-core fakes can drive a tool loop, and a scripted model that can."""
from typing import Any, Callable, Sequence
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel, FakeMessagesListChatModel, FakeListChatModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

@tool
def search_providers(service: str, area: str, date_range: str) -> str:
    """Search providers."""
    return '[{"id":"P1"}]'

tc_msg = AIMessage(content="", tool_calls=[{"name": "search_providers", "args": {"service": "plumber", "area": "Indiranagar", "date_range": "x"}, "id": "call_1", "type": "tool_call"}],
                   usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
for cls, kw in [(GenericFakeChatModel, {"messages": iter([tc_msg])}), (FakeMessagesListChatModel, {"responses": [tc_msg]}), (FakeListChatModel, {"responses": ["hi"]})]:
    m = cls(**kw)
    try:
        m.bind_tools([search_providers]); bt = "ok"
    except NotImplementedError as e:
        bt = "NotImplementedError"
    out = None
    if cls is not FakeListChatModel:
        r = cls(**kw).invoke([HumanMessage("x")])
        out = {"tool_calls": r.tool_calls, "usage": r.usage_metadata}
    print(cls.__name__, "bind_tools:", bt, "| invoke ->", out)
# streaming a tool call through GenericFakeChatModel
try:
    chunks = list(GenericFakeChatModel(messages=iter([tc_msg])).stream([HumanMessage("x")]))
    print("Generic stream chunks:", len(chunks))
except ValueError as e:
    print("Generic stream of a tool-call-only message FAILS:", e)
tc_msg2 = AIMessage(content="Searching.", tool_calls=tc_msg.tool_calls)
chunks = list(GenericFakeChatModel(messages=iter([tc_msg2])).stream([HumanMessage("x")]))
agg = chunks[0]
for c in chunks[1:]: agg = agg + c
print("Generic stream with text+tool_calls:", len(chunks), "chunks; aggregated tool_calls:", agg.tool_calls)


class ScriptedChatModel(BaseChatModel):
    """Deterministic model for offline evals. `script` is a callable(messages, tools) -> AIMessage
    (lets a scenario react to what the agent sent) or a list consumed in order. Records every request."""
    script: Any
    _calls: list = PrivateAttr(default_factory=list)
    _bound_tools: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any):
        if tool_choice in ("any",) or (tool_choice and tool_choice != "auto"):
            raise ValueError("forced tool_choice is rejected by claude-opus-5-5; the fake mirrors that")  # keep prod and fake honest
        self._bound_tools = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=self._bound_tools, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self._calls.append({"messages": list(messages), "tools": [t["function"]["name"] for t in kwargs.get("tools", [])]})
        if callable(self.script):
            msg = self.script(messages, kwargs.get("tools", []))
        else:
            msg = self.script[len(self._calls) - 1]
        return ChatResult(generations=[ChatGeneration(message=msg)])

def policy(messages, tools):
    last = messages[-1]
    if isinstance(last, HumanMessage):
        return tc_msg
    if isinstance(last, ToolMessage):
        return AIMessage("Found P1.", usage_metadata={"input_tokens": 150, "output_tokens": 10, "total_tokens": 160})
    return AIMessage("?")

fake = ScriptedChatModel(script=policy)
llm = fake.bind_tools([search_providers], tool_choice="auto")
msgs = [HumanMessage("find plumber")]
r = llm.invoke(msgs); msgs.append(r)
for tc in r.tool_calls:
    msgs.append(ToolMessage(search_providers.invoke(tc["args"]), tool_call_id=tc["id"]))
r2 = llm.invoke(msgs)
print("scripted loop:", r.tool_calls[0]["name"], "->", r2.content, "| recorded calls:", len(fake._calls), "tools seen:", fake._calls[0]["tools"])
try:
    fake.bind_tools([search_providers], tool_choice="any")
except ValueError as e:
    print("forced rejected by fake:", e)
