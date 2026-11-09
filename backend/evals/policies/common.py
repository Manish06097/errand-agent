"""Reading a planner request and writing scripted responses, shared by the policies.

A policy sees only the `ScriptedRequest`, exactly what a real model would get: the
system block, the user turn with its three sections, and the loop so far. Nothing
from the scenario reaches it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from errand.llm.scripted import ScriptedRequest

_SECTION = re.compile(r"<(errand_state|recent_turns|current_message)>\n(.*?)\n</\1>", re.S)
_SHORTLIST_ROW = re.compile(
    r"^- (?P<id>[A-Z]+\d+) (?P<name>.+?): ₹(?P<price>\d+), rating (?P<rating>[\d.]+)"
    r"(?P<verified>, verified)?; (?P<slots>.*)$"
)
_SLOT = re.compile(r"(?P<ref>S\d+) (?P<label>[A-Z][a-z]{2} \d+ [A-Z][a-z]{2}, \d\d:\d\d-\d\d:\d\d)")


@dataclass
class Candidate:
    provider_id: str
    name: str
    price: int
    rating: float
    first_slot: str | None
    verified: bool = False
    # (ref, label) for every slot handle shown, in order
    slots: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TurnView:
    current: str
    missing: list[str]
    known: dict[str, str]
    shortlist: list[Candidate]
    shortlist_current: bool
    # (tool name, parsed result or None, is_error) for every tool call answered so far
    loop: list[tuple[str, Any, bool]] = field(default_factory=list)
    # Record-backed lines of the errand state (M4)
    last_booking: str | None = None
    approval_card: str | None = None
    notes: list[str] = field(default_factory=list)
    recent: str = ""


def _text_of(message: HumanMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(
        str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _tool_data(text: str) -> Any:
    match = re.fullmatch(r"<tool_data>(.*)</tool_data>", text, re.S)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def read(request: ScriptedRequest) -> TurnView:
    human = next(m for m in request.messages if isinstance(m, HumanMessage))
    sections = dict(_SECTION.findall(_text_of(human)))
    state = sections.get("errand_state", "")
    missing: list[str] = []
    known: dict[str, str] = {}
    shortlist: list[Candidate] = []
    current_shortlist = False
    in_known = in_shortlist = False
    last_booking: str | None = None
    approval_card: str | None = None
    notes: list[str] = []
    for line in state.splitlines():
        if line.startswith("Last booking in this chat: "):
            last_booking = line.removeprefix("Last booking in this chat: ")
            in_known = False
        elif line.startswith("Approval card: "):
            approval_card = line.removeprefix("Approval card: ")
            in_known = False
        elif line.startswith("App note: "):
            notes.append(line.removeprefix("App note: "))
            in_known = False
        elif line.startswith("Missing:"):
            rest = line.removeprefix("Missing:").strip()
            missing = [] if rest == "none" else [p.strip() for p in rest.split(",")]
            in_known = in_shortlist = False
        elif line == "Known:":
            in_known = True
        elif line.startswith("Shortlist ("):
            in_shortlist, in_known = True, False
            current_shortlist = "from an earlier brief" not in line
        elif in_known and line.startswith("- ") and ":" in line:
            name, _, value = line[2:].partition(":")
            known[name.strip()] = value.strip()
        elif in_shortlist:
            match = _SHORTLIST_ROW.match(line)
            if match:
                slots = match.group("slots")
                first = slots.split(";")[0].strip() if slots else None
                shortlist.append(
                    Candidate(
                        provider_id=match.group("id"),
                        name=match.group("name"),
                        price=int(match.group("price")),
                        rating=float(match.group("rating")),
                        first_slot=first,
                        verified=bool(match.group("verified")),
                        slots=[(m.group("ref"), m.group("label")) for m in _SLOT.finditer(slots)],
                    )
                )
    names: dict[str, str] = {}
    loop: list[tuple[str, Any, bool]] = []
    # A compacted or rebuilt request (N2B) carries this run's earlier tool results as text.
    carried = re.search(r"<run_so_far>\n(.*?)\n</run_so_far>", _text_of(human), re.S)
    if carried is not None:
        for row in carried.group(1).splitlines():
            found = re.match(r"\[([a-z_]+)\] (<tool_data>.*</tool_data>|.*)$", row)
            if found is not None:
                data = _tool_data(found.group(2))
                loop.append((found.group(1), data, isinstance(data, dict) and "error" in data))
    for m in request.messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                names[str(tc["id"])] = str(tc["name"])
        elif isinstance(m, ToolMessage):
            text = m.content if isinstance(m.content, str) else json.dumps(m.content)
            loop.append((names.get(m.tool_call_id, "?"), _tool_data(text), m.status == "error"))
    return TurnView(
        current=sections.get("current_message", ""),
        missing=missing,
        known=known,
        shortlist=shortlist,
        shortlist_current=current_shortlist and bool(shortlist),
        loop=loop,
        last_booking=last_booking,
        approval_card=approval_card,
        notes=notes,
        recent=sections.get("recent_turns", ""),
    )


def _usage(request: ScriptedRequest, output: int) -> dict[str, int]:
    # Deterministic and roughly what the API would report for this request.
    size = len(json.dumps(request.canonical, ensure_ascii=False).encode("utf-8"))
    tokens_in = math.ceil(size / 3.5)
    return {"input_tokens": tokens_in, "output_tokens": output, "total_tokens": tokens_in + output}


def _thinking(request: ScriptedRequest, note: str) -> dict[str, Any]:
    return {
        "type": "thinking",
        "thinking": note,
        "signature": f"scripted-signature-{request.n:04d}",
    }


def tool_calls(
    request: ScriptedRequest, note: str, *calls: tuple[str, dict[str, Any]]
) -> AIMessage:
    blocks: list[str | dict[str, Any]] = [_thinking(request, note)]
    parsed = []
    for i, (name, args) in enumerate(calls):
        call_id = f"toolu_{request.n:03d}_{i}"
        blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
        parsed.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
    return AIMessage(
        content=blocks,
        tool_calls=parsed,
        usage_metadata=_usage(request, 60 + 20 * len(calls)),
        response_metadata={"stop_reason": "tool_use", "model_name": "claude-opus-5-5"},
    )


def answer(request: ScriptedRequest, text: str) -> AIMessage:
    return AIMessage(
        content=[{"type": "text", "text": text}],
        usage_metadata=_usage(request, 30 + len(text) // 4),
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5"},
    )


def answer_with_thinking(request: ScriptedRequest, text: str, note: str = "Say it.") -> AIMessage:
    """A final answer that carries a thinking block before its text, as a real model's does.
    The block has a signature, so a history edit would be caught by the prefix check."""
    return AIMessage(
        content=[_thinking(request, note), {"type": "text", "text": text}],
        usage_metadata=_usage(request, 30 + len(text) // 4),
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5"},
    )
