"""Gathering S3 to S6 from state, and rebuilding a request from it (13.1, 13.3, decision 11).

`gather` reads what the pure builder needs: the user's memory rows and epoch, the ledger,
the redactions and the forgotten-value mask. `rebuild` makes a fresh request from state:
used when the memory epoch moved between two loop calls (a forget or a correct landed) and
by compaction when the next call would cross the cap. A rebuilt request is always a new
`LoopSession`, never an edit of the old one, because the session requires each request to
extend the previous one byte for byte (thinking signatures are bound to that prefix).

Compaction drops thinking and tool_use blocks and carries this run's tool results as text.
If it still does not fit, sections shed in a fixed order: S6 to the latest turn, then S5,
then S4 to its pinned rows. S1, S2, S3 and S7 never shed. No model ever summarises.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from errand.clock import from_ms, to_ms
from errand.config import MemorySettings
from errand.context.builder import (
    CAPS,
    BuiltContext,
    MemoryInputs,
    StateExtras,
    build_context,
)
from errand.context.ledger import build_ledger
from errand.context.memory_section import build_memory_section
from errand.domain.errand import Errand
from errand.domain.inbound import UserMessage
from errand.llm.tokens import est_request
from errand.memory.mask import mask_text
from errand.store.agent_db import AgentDB
from errand.store.repos.known_bookings import KnownBooking
from errand.store.repos.memories import MemoryRow
from errand.store.repos.messages import Message

# The sheds, in order. Each level includes the ones before it.
SHED_ORDER: tuple[str, ...] = ("recent", "ledger", "memory")
UPCOMING_SHOWN = 5
REDACTED_TAIL = 80  # messages whose redactions are looked up: more than S6 can render


@dataclass(frozen=True)
class Gathered:
    """Everything read once per build; `inputs(shed)` renders it for a shed level."""

    epoch: int
    rows: tuple[MemoryRow, ...]
    bookings: tuple[KnownBooking, ...]
    redactions: dict[str, list[tuple[int, int, str]]]
    mask: Callable[[str], tuple[str, int]] | None
    errand_kind: str
    service: str | None
    prefilled: dict[str, str]  # memory_id -> brief field it filled
    now_ms: int
    tz: tzinfo
    half_life_days: float
    names: dict[str, str]

    def inputs(self, shed: tuple[str, ...] = (), *, compacted: bool = False) -> MemoryInputs:
        cap = 0 if "memory" in shed else CAPS["memory"]
        text, selection = build_memory_section(
            self.rows,
            errand_kind=self.errand_kind,
            service=self.service,
            now_ms=self.now_ms,
            half_life_days=self.half_life_days,
            cap_tokens=cap,
        )
        included = tuple(
            {"id": c.memory_id, "score": c.score, "prefill": self.prefilled.get(c.memory_id)}
            for c in selection.included
        )
        ledger_text, lines = "", 0
        if "ledger" not in shed:
            upcoming = self.upcoming_ids()
            ledger = build_ledger(
                self.bookings,
                tz=self.tz,
                names=self.names,
                cap_tokens=CAPS["ledger"],
                exclude=upcoming,
            )
            ledger_text, lines = ledger.text, ledger.lines
        return MemoryInputs(
            epoch=self.epoch,
            memory_text=text,
            included=included,
            excluded=selection.excluded,
            ledger_text=ledger_text,
            ledger_lines=lines,
            redactions=self.redactions,
            mask=self.mask,
            shed=shed,
            compacted=compacted,
        )

    def upcoming_ids(self) -> frozenset[str]:
        return frozenset(b.booking_id for b in self._upcoming())

    def _upcoming(self) -> list[KnownBooking]:
        return sorted(
            (b for b in self.bookings if b.status == "confirmed" and b.slot_start > self.now_ms),
            key=lambda b: (b.slot_start, b.booking_id),
        )

    def upcoming_lines(self) -> tuple[tuple[str, ...], int]:
        """Upcoming bookings of the errand's service, newest sub-cap, and a count of the rest."""
        mine = [b for b in self._upcoming() if self.service is None or b.service == self.service]
        shown = mine[:UPCOMING_SHOWN]
        lines = tuple(
            f"{b.booking_id}: {b.service.replace('_', ' ')} with "
            f"{self.names.get(b.provider_id, b.provider_id)}, "
            f"{from_ms(b.slot_start).astimezone(self.tz):%a %d %b %H:%M}"
            for b in shown
        )
        return lines, len(self._upcoming()) - len(shown)


async def gather(
    db: AgentDB,
    *,
    user_id: str,
    errand: Errand,
    history: Sequence[Message],
    now: datetime,
    tz: tzinfo,
    mask_key: bytes | None,
    names: dict[str, str] | None = None,
) -> Gathered:
    async with db.session() as repos:
        rows = await repos.memories.for_user(user_id)
        epoch = await repos.memories.epoch(user_id)
        bookings = await repos.known_bookings.for_user(user_id)
        hashes = await repos.memories.hashes_for_user(user_id) if mask_key else {}
        tail = [m.message_id for m in history[-REDACTED_TAIL:]]
        redactions: dict[str, list[tuple[int, int, str]]] = {}
        for r in await repos.memories.redactions_for_messages(tail):
            if r.kind == "superseded":
                redactions.setdefault(r.message_id, []).append((r.start, r.end, r.kind))
    mask: Callable[[str], tuple[str, int]] | None = None
    if mask_key and hashes:
        key = mask_key

        def mask(text: str) -> tuple[str, int]:
            result = mask_text(text, key, hashes)
            return result.text, result.masked

    brief = errand.brief
    prefilled: dict[str, str] = {}
    for name in ("area", "budget_max_inr", "window"):
        fact = getattr(brief, name, None)
        prov = getattr(fact, "prov", None)
        if prov is not None and prov.source == "memory" and prov.memory_id:
            prefilled[prov.memory_id] = name
    pref = getattr(brief, "provider_pref", None)
    for pref_id in (
        getattr(pref, "prefer_memory_id", None),
        getattr(pref, "avoid_memory_id", None),
    ):
        if pref_id:
            prefilled[pref_id] = "provider_pref"
    for bound in brief_window_bounds(brief):
        prefilled.setdefault(bound, "window")
    return Gathered(
        epoch=epoch,
        rows=tuple(rows),
        bookings=tuple(bookings),
        redactions=redactions,
        mask=mask,
        errand_kind=errand.kind.value,
        service=brief.service.value.value if brief.service else None,
        prefilled=prefilled,
        now_ms=to_ms(now),
        tz=tz,
        half_life_days=MemorySettings().half_life_days,
        names=names or {},
    )


def brief_window_bounds(brief: Any) -> list[str]:
    """Memory ids behind a window's time bounds (N2B 2.4, 13.7)."""
    window = getattr(brief, "window", None)
    ids = getattr(window, "bound_memory_ids", None)
    return list(ids.values()) if ids else []


def build_with(
    gathered: Gathered,
    *,
    errand: Errand,
    history: Sequence[Message],
    current: UserMessage,
    now: datetime,
    tz: tzinfo,
    tools: Sequence[dict[str, Any]],
    extras: StateExtras | None,
    shed: tuple[str, ...] = (),
    tool_results: Sequence[str] = (),
    compacted: bool = False,
) -> BuiltContext:
    lines, other = gathered.upcoming_lines()
    base = extras or StateExtras()
    with_upcoming = StateExtras(
        last_booking=base.last_booking,
        approval_card=base.approval_card,
        notes=base.notes,
        upcoming=lines,
        other_upcoming=other,
    )
    return build_context(
        errand=errand,
        history=history,
        current=current,
        now=now,
        tz=tz,
        tools=tools,
        extras=with_upcoming,
        memory=gathered.inputs(shed, compacted=compacted),
        tool_results=tool_results,
    )


def request_estimate(built: BuiltContext, tools_json: str) -> int:
    """The gateway's estimate for this built context as a first request."""
    system = [b["text"] for b in built.system]
    return est_request(system, built.messages, tools_json)


def compacted(
    gathered: Gathered,
    *,
    errand: Errand,
    history: Sequence[Message],
    current: UserMessage,
    now: datetime,
    tz: tzinfo,
    tools: Sequence[dict[str, Any]],
    tools_json: str,
    extras: StateExtras | None,
    tool_results: Sequence[str],
    budget: int,
    reserve: int = 0,
) -> BuiltContext | None:
    """The first rebuild that fits `budget - reserve`, shedding in the fixed order; None if
    even the last level does not fit (the caller ends the run CONTEXT_EXHAUSTED)."""
    levels: list[tuple[str, ...]] = [()]
    for i in range(1, len(SHED_ORDER) + 1):
        levels.append(SHED_ORDER[:i])
    last: BuiltContext | None = None
    for shed in levels:
        last = build_with(
            gathered,
            errand=errand,
            history=history,
            current=current,
            now=now,
            tz=tz,
            tools=tools,
            extras=extras,
            shed=shed,
            tool_results=tool_results,
            compacted=True,
        )
        if request_estimate(last, tools_json) <= budget - reserve:
            return last
    return None
