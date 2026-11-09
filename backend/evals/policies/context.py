"""Scripted planners for the memory and context scenarios (N2B).

`memory_policy` is the reference planner plus the memory tools: it calls `remember` when
the user's message states a standing preference in the words the product's cues look for,
`list_memories` for "what do you remember about me", and otherwise defers to the reference
planner. `growing_policy` grows the loop on purpose (S42d): it asks `list_bookings` again
and again, four calls per turn, as many times as the message says (`grow=N`), then answers.

`hooked_factory` wraps a policy in a model that awaits an async hook before each call. The
harness uses it to forget a memory between two loop calls of one run (S49b) through the
real DELETE route; no product code carries a trigger.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from errand.llm.scripted import Policy, ScriptedChatModel, ScriptedRequest
from evals.policies import reference
from evals.policies.common import answer, read, tool_calls

# (pattern over the user's message, key, scope, which group is the value)
_REMEMBER: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"from now on,? tell them (?:to )?(?P<v>[^.!?]+)", re.I),
        "access_note",
        "global",
    ),
    (re.compile(r"\bi(?:'ve| have)? moved to (?P<v>[A-Za-z]+)", re.I), "home_area", "global"),
    (re.compile(r"\bi live in (?P<v>[A-Za-z]+)", re.I), "home_area", "global"),
    (
        re.compile(r"from now on,? never (?:book )?before (?P<v>\d{1,2}(?::\d\d)?\s*[ap]m)", re.I),
        "earliest_time",
        "global",
    ),
    (
        re.compile(r"\bthis time (?P<v>mornings only)", re.I),
        "latest_time",
        "global",
    ),
)
_REMEMBER_AREA = re.compile(r"\bmoved to (?P<v>[A-Za-z]+)", re.I)
_GROW = re.compile(r"grow=(\d+)")


def memory_policy(request: ScriptedRequest) -> AIMessage:
    view = read(request)
    step = len(view.loop)
    low = view.current.lower()
    if "what do you remember" in low:
        if step == 0:
            return tool_calls(request, "Show what is saved.", ("list_memories", {}))
        return answer(request, "That card shows everything I have saved about you.")
    if step == 0:
        for pattern, key, scope in _REMEMBER:
            found = pattern.search(view.current)
            if found is None:
                continue
            value = found.group("v").strip()
            if key == "latest_time":
                value = "12:00"
            args = {"key": key, "scope": scope, "value": value, "quote": found.group(0)}
            return tool_calls(request, "Save the preference.", ("remember", args))
    if step == 1 and view.loop[-1][0] == "remember" and view.loop[-1][2] is False:
        saved = _REMEMBER_AREA.search(view.current)
        if (
            saved is not None
            and view.known.get("area", "").lower().split(" ")[0] != saved.group("v").lower()
        ):
            args = {
                "set": {"area": saved.group("v").lower()},
                "clear": [],
                "evidence": [{"field": "area", "quote": saved.group("v")}],
                "not_covered": None,
            }
            return tool_calls(request, "Use it for this errand too.", ("update_brief", args))
    memory_calls = {"remember", "list_memories"}
    if step >= 1 and view.loop and view.loop[-1][0] in memory_calls and _only_a_preference(view):
        return answer(request, "Saved. I'll use that from now on.")
    return reference.policy(_without(request, memory_calls))


def _without(request: ScriptedRequest, names: set[str]) -> ScriptedRequest:
    """The request as the reference planner knows it: the memory tool calls (and their
    results, also when a rebuild carried them as text) are not part of its loop."""
    ids = {
        str(tc["id"])
        for m in request.messages
        if isinstance(m, AIMessage)
        for tc in m.tool_calls
        if tc["name"] in names
    }
    kept: list[BaseMessage] = []
    for m in request.messages:
        if isinstance(m, AIMessage) and ids and any(str(tc["id"]) in ids for tc in m.tool_calls):
            continue
        if isinstance(m, ToolMessage) and m.tool_call_id in ids:
            continue
        if isinstance(m, HumanMessage) and "<run_so_far>" in str(m.content):
            m = _drop_carried(m, names)
        kept.append(m)
    return replace(request, messages=tuple(kept))


def _drop_carried(message: HumanMessage, names: set[str]) -> HumanMessage:
    blocks = message.content if isinstance(message.content, list) else [message.content]
    out: list[Any] = []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else block
        if isinstance(text, str) and "<run_so_far>" in text:
            lines = [
                ln for ln in text.splitlines() if not any(ln.startswith(f"[{n}] ") for n in names)
            ]
            text = "\n".join(lines)
            if "<run_so_far>\n</run_so_far>" in text:
                text = ""
            block = {"type": "text", "text": text} if isinstance(block, dict) else text
        if text != "":
            out.append(block)
    return HumanMessage(content=out)


def _only_a_preference(view: Any) -> bool:
    """A message that states a preference and asks for no errand."""
    text = view.current.lower()
    return not re.search(r"\b(book|find|need|plumber|electrician|ac)\b", text)


def growing_policy(request: ScriptedRequest) -> AIMessage:
    view = read(request)
    found = _GROW.search(view.current)
    if found is None:
        return reference.policy(request)
    total = int(found.group(1))
    done = len(view.loop)
    if done >= total:
        return answer(request, "That is everything I could find about your bookings.")
    batch: list[tuple[str, dict[str, Any]]] = [("list_bookings", {})] * min(4, total - done)
    return tool_calls(request, "Look at the bookings again.", *batch)


class HookedScripted(ScriptedChatModel):
    """A scripted model that awaits `hook(n)` before the Nth request of its run is recorded.

    The hook runs after the gateway's epoch probe for call N, so a forget it makes lands
    "during" that call: the call finishes, and the probe of call N+1 sees the change.
    """

    hook: Callable[[int], Awaitable[None]] | None = None

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Any:
        if self.hook is not None:
            await self.hook(len(self._requests) + 1)
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            assert isinstance(chunk, ChatGenerationChunk)
            yield chunk


def hooked_factory(policy: Policy, hook: Callable[[int], Awaitable[None]]) -> Any:
    def make() -> HookedScripted:
        return HookedScripted(script=policy, hook=hook)

    return make
