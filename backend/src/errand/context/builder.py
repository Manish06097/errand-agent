"""The minimal context builder (architecture 13, ADR 0007). M12 grows it to every section.

`build_context` is a pure function of the errand, the transcript, the current message
and the clock reading: no ids, no wall clock, no set order, so the same inputs give
the same request bytes (replay hashes and the eval table depend on that).

Sections and caps (estimated tokens, 13.1):
- S1 rules: `prompts/rules.md`, the only system block, with `cache_control`. It is the
  same bytes on every turn.
- S2 tools: the fixed planner tool list (measured here, sent by the gateway).
- S3 errand state: the brief with provenance, `Known:` and `Missing:`, the ask
  history, and a digest of the current shortlist.
- S6 recent turns: up to the last 6 turns as text, whole turns, newest first until the
  cap, never a thinking block.
- S7 current message: must fit whole; over its cap the run ends CONTEXT_TOO_LARGE and
  nothing is sent.
Everything dynamic goes in the one user turn, never in the system block.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from importlib import resources
from typing import Any

from langchain_core.messages import HumanMessage

from errand.agent.brief_ops import known_lines, missing
from errand.domain.errand import Errand
from errand.domain.inbound import UserMessage
from errand.domain.messages import ClarifyCard, OutcomeNotice, ShortlistCard, TextBody
from errand.domain.windows import fmt_day, slot_label
from errand.llm.tokens import est_tokens
from errand.store.repos.messages import Message

CAPS: dict[str, int] = {
    "rules": 1_600,
    "tools": 2_000,
    "state": 1_100,
    "recent": 1_800,
    "current": 1_000,
}
RECENT_TURNS = 6
SHORTLIST_SHOWN = 5


def rules_text() -> str:
    return resources.files("errand.agent").joinpath("prompts/rules.md").read_text("utf-8")


def system_blocks() -> list[dict[str, Any]]:
    return [{"type": "text", "text": rules_text(), "cache_control": {"type": "ephemeral"}}]


@dataclass(frozen=True)
class SectionOverCap:
    section: str
    estimated: int
    cap: int


@dataclass(frozen=True)
class BuiltContext:
    system: list[dict[str, Any]]
    messages: list[HumanMessage]
    manifest: dict[str, Any]
    over_cap: SectionOverCap | None = None
    text_blocks: list[str] = field(default_factory=list)


def build_context(
    *,
    errand: Errand,
    history: Sequence[Message],
    current: UserMessage,
    now: datetime,
    tz: tzinfo,
    tools: Sequence[dict[str, Any]],
) -> BuiltContext:
    system = system_blocks()
    rules_est = est_tokens(system[0]["text"])
    tools_est = est_tokens(json.dumps(list(tools), ensure_ascii=False, separators=(",", ":")))
    state = "<errand_state>\n" + render_state(errand, now, tz) + "\n</errand_state>"
    recent_text, included, dropped = _render_recent(history, current.message_id)
    recent = "<recent_turns>\n" + (recent_text or "(none)") + "\n</recent_turns>"
    current_text = "<current_message>\n" + current.text + "\n</current_message>"
    sections = {
        "rules": rules_est,
        "tools": tools_est,
        "state": est_tokens(state),
        "recent": est_tokens(recent),
        "current": est_tokens(current_text),
    }
    manifest: dict[str, Any] = {
        "builder": "context.builder",
        "sections": sections,
        "caps": dict(CAPS),
        "messages_included": included,
        "turns_dropped": dropped,
        "current_message_id": current.message_id,
    }
    over = next(
        (
            SectionOverCap(name, sections[name], CAPS[name])
            for name in ("current", "state", "rules", "tools")
            if sections[name] > CAPS[name]
        ),
        None,
    )
    blocks = [state, recent, current_text]
    content: list[str | dict[str, Any]] = [{"type": "text", "text": b} for b in blocks]
    return BuiltContext(
        system=system,
        messages=[HumanMessage(content=content)],
        manifest=manifest,
        over_cap=over,
        text_blocks=blocks,
    )


def render_state(errand: Errand, now: datetime, tz: tzinfo) -> str:
    brief = errand.brief
    local = now.astimezone(tz)
    lines = [
        f"Now: {fmt_day(local.date())} {local.year}, {local:%H:%M} IST",
        f"Errand: {errand.kind.value} (phase: {errand.phase.value}, brief v{brief.version})",
        "Known:",
    ]
    known = known_lines(brief)
    lines += [f"- {k}" for k in known] or ["- (nothing yet)"]
    gaps = missing(brief, errand.kind)
    lines.append("Missing: " + (", ".join(f.value for f in gaps) if gaps else "none"))
    if brief.asks:
        asked: dict[str, list[str]] = {}
        for record in brief.asks:
            note = f"turn {record.turn}" + (" with options" if record.with_options else "")
            asked.setdefault(record.field.value, []).append(note)
        lines.append(
            "Asked before: " + "; ".join(f"{k} ({', '.join(v)})" for k, v in asked.items())
        )
    if brief.ask_exhausted:
        lines.append("Do not ask again: " + ", ".join(f.value for f in brief.ask_exhausted))
    lines += _render_shortlist(errand, tz)
    return "\n".join(lines)


def _render_shortlist(errand: Errand, tz: tzinfo) -> list[str]:
    shortlist = errand.shortlist
    if shortlist is None:
        return ["Shortlist: none yet"]
    stale = shortlist.brief_version != errand.brief.version
    head = f"Shortlist (brief v{shortlist.brief_version}, {shortlist.window_label})"
    if stale:
        head += ", from an earlier brief: search again before recommending"
    lines = [head + ":"]
    for c in shortlist.candidates[:SHORTLIST_SHOWN]:
        slots = "; ".join(f"{h.ref} {slot_label(h.slot, tz)}" for h in c.slots)
        verified = ", verified" if c.verified else ""
        lines.append(
            f"- {c.provider_id} {c.name}: ₹{c.price_inr}, rating {c.rating}{verified}; {slots}"
        )
    more = len(shortlist.candidates) - SHORTLIST_SHOWN
    if more > 0:
        lines.append(f"- and {more} more")
    return lines


def render_body(body: dict[str, Any]) -> str:
    kind = body.get("type")
    if kind == "text":
        return TextBody.model_validate(body).text
    if kind == "clarify":
        card = ClarifyCard.model_validate(body)
        options = f" Options: {', '.join(card.options)}." if card.options else ""
        return f"[asked for {', '.join(f.value for f in card.fields)}] {card.question}{options}"
    if kind == "shortlist":
        sl = ShortlistCard.model_validate(body)
        names = ", ".join(c.name for c in sl.candidates)
        rec = f" {sl.recommendation}" if sl.recommendation else ""
        return f"[showed {len(sl.candidates)} options for {sl.window_label}: {names}]{rec}"
    if kind == "notice":
        return OutcomeNotice.model_validate(body).text
    return ""


def _render_recent(history: Sequence[Message], current_id: str) -> tuple[str, list[str], int]:
    """Whole turns, newest first, until S6's cap; returned oldest first."""
    turns: list[list[Message]] = []
    for m in history:
        if m.message_id == current_id:
            continue
        if m.role == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    turns = turns[-RECENT_TURNS:]
    kept: list[list[Message]] = []
    used = est_tokens("<recent_turns>\n\n</recent_turns>")
    for turn in reversed(turns):
        cost = sum(est_tokens(_line(m)) for m in turn)
        if used + cost > CAPS["recent"]:
            break
        kept.append(turn)
        used += cost
    kept.reverse()
    lines = [_line(m) for turn in kept for m in turn]
    ids = [m.message_id for turn in kept for m in turn]
    return "\n".join(lines), ids, len(turns) - len(kept)


def _line(m: Message) -> str:
    who = "user" if m.role == "user" else "assistant"
    return f"{who}: {render_body(m.body)}"
