"""The planner's memory tools: `remember`, `list_memories` and `propose_forget` (10.3, 10.7).

Schemas are literals, bound after the other eight tools and never filtered. The handlers
live here so `plan.py` only dispatches to them.

- `remember(key, scope, value, quote)` has no claim argument (gate 2 decision 4): code
  checks the quote against the user's current message (`memory.evidence`). A refusal quotes
  the user's words back and writes `guard.evidence_rejected`. Without a standing-preference
  cue, a value that fits the open errand becomes a brief patch plus a "Remember this for
  next time?" chip; a one-off ("this time") is only refused, and shows no chip.
- `list_memories()` renders a `MemoryListCard` for the user; the model gets a fixed string
  and no value (S64).
- `propose_forget(memory_ref)` renders a forget chip and answers with a fixed string. A
  forget is the user's click; no path from a model call reaches it (S61's rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from errand.clock import to_ms
from errand.domain.messages import (
    MemoryForgetChip,
    MemoryListCard,
    MemoryOfferChip,
    MemorySavedChip,
    MemoryUpdateChip,
    MessageBody,
)
from errand.memory.service import MemoryRefused
from errand.store.repos.messages import Message
from errand.tools import views

if TYPE_CHECKING:
    from errand.agent.deps import RunDeps
    from errand.agent.nodes.plan import PlannerLoop

MEMORY_KEYS = [
    "home_area",
    "earliest_time",
    "latest_time",
    "avoid_days",
    "budget_ceiling",
    "preferred_provider",
    "avoid_provider",
    "language",
    "access_note",
]


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


REMEMBER: dict[str, Any] = {
    "name": "remember",
    "description": (
        "Save a standing preference (always, never, from now on). scope: global or "
        "service:<name>. Times HH:MM."
    ),
    "input_schema": _object(
        {
            "key": {"type": "string", "enum": MEMORY_KEYS},
            "scope": {"type": "string"},
            "value": {"type": "string"},
            "quote": {"type": "string"},
        }
    ),
}
LIST_MEMORIES: dict[str, Any] = {
    "name": "list_memories",
    "description": "Show the user their memories; you see nothing.",
    "input_schema": _object({}),
}
PROPOSE_FORGET: dict[str, Any] = {
    "name": "propose_forget",
    "description": "Offer to forget a <memory> id; the user confirms.",
    "input_schema": _object({"id": {"type": "string"}}),
}
MEMORY_TOOLS: tuple[dict[str, Any], ...] = (REMEMBER, LIST_MEMORIES, PROPOSE_FORGET)
MEMORY_TOOL_NAMES = frozenset(t["name"] for t in MEMORY_TOOLS)

SHOWN = "Shown to the user."
OFFERED = "Offered to the user."


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    scope: str = Field(min_length=1, max_length=60)
    value: str
    quote: str = Field(min_length=1, max_length=400)


class ProposeForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)


# The brief field a value can be set through when `remember` had no cue (decision 3).
_FITS_BRIEF = {"home_area": "area", "budget_ceiling": "budget_max_inr"}


async def post_chip(deps: RunDeps, body: MessageBody, *, turn_no: int | None, seq: int) -> None:
    """Write a memory chip into the conversation as an assistant message of no run kind.

    The one-per-run-and-kind rule of `respond` is about the outcome message; a chip is an
    extra line in the transcript, so it carries no run_id and is ordered by creation.
    """
    async with deps.db.session() as repos:
        await repos.messages.insert(
            Message(
                message_id=f"{deps.run_id}-mc{seq}",
                conversation_id=deps.conversation_id,
                client_msg_id=None,
                role="assistant",
                kind="card",
                body=body.model_dump(mode="json"),
                run_id=None,
                turn_no=turn_no,
                approval_id=None,
                typed_grant_ok=None,
                rendered_at=None,
                created_at=to_ms(deps.clock.now()),
            )
        )


class MemoryTools:
    def __init__(self, loop: PlannerLoop) -> None:
        self.loop = loop
        self.deps = loop.deps

    async def chip(self, body: MessageBody) -> None:
        self.deps.chip_seq += 1
        await post_chip(self.deps, body, turn_no=self.loop.inbound.turn_no, seq=self.deps.chip_seq)

    async def handle(self, name: str, args: Mapping[str, Any]) -> tuple[str, bool]:
        if self.deps.memory is None:
            return views.render_error("memory_unavailable"), True
        if name == "remember":
            return await self.remember(args)
        if name == "list_memories":
            return await self.list_memories(args)
        return await self.propose_forget(args)

    # ------------------------------------------------------------------ remember

    async def remember(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        loop, deps = self.loop, self.deps
        assert deps.memory is not None
        try:
            parsed = RememberArgs.model_validate(dict(args))
        except ValidationError:
            return views.render_error("invalid_arguments"), True
        value: Any = parsed.value
        if parsed.key in ("avoid_days", "avoid_provider"):
            value = [v for v in re.split(r"[,;]|\band\b|\s", value) if v.strip()]
        try:
            saved = await deps.memory.remember_explicit(
                user_id=deps.user_id,
                key=parsed.key,
                scope=parsed.scope,
                value=value,
                message_id=loop.inbound.message_id,
                quote=parsed.quote,
                run_id=deps.run_id,
            )
        except MemoryRefused as exc:
            return await self._refused(parsed, exc)
        row = saved["memory"]
        deps.tracer.emit(
            "memory.written",
            memory_id=row["memory_id"],
            kind=row["kind"],
            status=row["status"],
            value_hash=deps.memory.hash_value(row["value"]),
        )
        await self.chip(MemorySavedChip(memory_id=row["memory_id"]))
        return views.render({"ok": True, "saved": parsed.key}), False

    async def _refused(self, parsed: RememberArgs, exc: MemoryRefused) -> tuple[str, bool]:
        loop, deps = self.loop, self.deps
        deps.tracer.emit(
            "guard.evidence_rejected",
            detail={
                "tool": "remember",
                "fields": [parsed.key],
                "message_id": loop.inbound.message_id,
                "problems": 1,
                "reason": exc.reason,
            },
        )
        extra: dict[str, Any] = {}
        if exc.reason == "no_cue" and parsed.key in _FITS_BRIEF:
            patched = await self._patch_and_offer(parsed)
            if patched:
                extra["applied_to_this_errand"] = True
        return views.render_error(
            "remember_refused",
            reason=exc.reason,
            detail=exc.detail,
            user_message=loop.inbound.text,
            **extra,
        ), True

    async def _patch_and_offer(self, parsed: RememberArgs) -> bool:
        """No cue: the value goes into this errand's brief through update_brief (its own
        evidence check), and a chip offers to remember it. The chip holds no value."""
        loop = self.loop
        field = _FITS_BRIEF[parsed.key]
        value: Any = parsed.value
        if field == "budget_max_inr":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return False
        patch = {
            "set": {field: value},
            "clear": [],
            "evidence": [{"field": field, "quote": parsed.quote}],
            "not_covered": None,
        }
        out = await loop.update_brief_quiet(patch)
        if not out:
            return False
        from errand.memory.evidence import locate_quote  # local: evidence is a leaf module

        located = locate_quote(loop.inbound.text, parsed.quote)
        if located is None:
            return False
        await self.chip(
            MemoryOfferChip(
                key=parsed.key,
                scope=parsed.scope,
                source_message_id=loop.inbound.message_id,
                start=located.start,
                end=located.end,
            )
        )
        return True

    # ------------------------------------------------------------------ list, forget

    async def list_memories(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        deps = self.deps
        if args:
            return views.render_error("no_arguments"), True
        async with deps.db.session() as repos:
            rows = await repos.memories.for_user(deps.user_id)
        card = MemoryListCard(
            memory_ids=[r.memory_id for r in rows if r.status == "active"],
            suggestion_ids=[r.memory_id for r in rows if r.status == "pending_confirmation"],
        )
        await self.chip(card)
        return SHOWN, False

    async def propose_forget(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        deps = self.deps
        try:
            parsed = ProposeForgetArgs.model_validate(dict(args))
        except ValidationError:
            return views.render_error("invalid_arguments"), True
        async with deps.db.session() as repos:
            row = await repos.memories.get(parsed.id)
        if row is None or row.user_id != deps.user_id or row.status != "active":
            self.loop.reject("propose_forget", "unknown_memory_ref")
            return views.render_error(
                "unknown_memory",
                detail="use an id shown in <memory>; nothing else can be forgotten here",
            ), True
        await self.chip(MemoryForgetChip(memory_id=row.memory_id))
        return OFFERED, False


# brief field -> (memory key, scope of that key for a brief's service)
_UPDATABLE = {"area": ("home_area", False), "budget_max_inr": ("budget_ceiling", True)}


async def offer_updates(
    loop: PlannerLoop, old: Any, new: Any, quotes: Mapping[str, list[str]]
) -> None:
    """A chat correction of a memory-filled field changes this errand only; a chip offers
    "Update your saved ...? [Update] [Just this time]" (S33). The chip holds the span of
    the user's message, never the value, so a later forget leaves no copy in it."""
    from errand.memory.evidence import locate_quote

    for name, (key, service_scoped) in _UPDATABLE.items():
        before, after = getattr(old, name, None), getattr(new, name, None)
        if before is None or after is None or before is after:
            continue
        if before.prov.source != "memory" or before.prov.memory_id is None:
            continue
        if after.prov.source != "user_msg" or before.value == after.value:
            continue
        spans = quotes.get(name) or []
        located = locate_quote(loop.inbound.text, spans[0]) if spans else None
        if located is None:
            continue
        scope = "global"
        if service_scoped and new.service is not None:
            scope = f"service:{new.service.value.value}"
        await loop.memory_tools.chip(
            MemoryUpdateChip(
                memory_id=before.prov.memory_id,
                key=key,
                scope=scope,
                source_message_id=loop.inbound.message_id,
                start=located.start,
                end=located.end,
            )
        )
