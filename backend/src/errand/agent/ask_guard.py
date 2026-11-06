"""The ask_user guard: ask once, re-ask once with options, then stop (7.3, 10.2).

Enforced here, in code, on every ask_user call. The tool itself is never taken out of
the planner's tool list mid-loop: the tools are part of the prefix that thinking
signatures are bound to (spike C1).

Rules, in the order they are checked:
1. at least one field, no duplicates;
2. every field is in `missing()` (a filled field is never asked);
3. a field asked twice already, or whose budget is spent, ends the run: ASKED_USER
   with a code message naming what is still needed, and the field is marked spent so
   it is never asked again in this errand;
4. every missing field whose budget is not spent is in this one question;
5. a re-ask (the second ask of a field) carries quick-reply options. Being in
   `missing()` means the user's reply after the first ask did not fill it.
A rejection under 1, 2, 4 or 5 is a tool error the planner can correct.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from errand.agent.brief_ops import missing
from errand.domain.brief import AskRecord, Brief, BriefField, ErrandKind

MAX_ASKS_PER_FIELD = 2


@dataclass(frozen=True)
class Accept:
    brief: Brief  # with the asks recorded


@dataclass(frozen=True)
class Reject:
    reason: str


@dataclass(frozen=True)
class Exhausted:
    brief: Brief  # with the spent fields recorded
    spent: list[BriefField]
    still_needed: list[BriefField]


AskDecision = Accept | Reject | Exhausted


def ask_counts(brief: Brief) -> Counter[BriefField]:
    return Counter(record.field for record in brief.asks)


def check_ask(
    brief: Brief,
    kind: ErrandKind,
    fields: Sequence[BriefField],
    options: Sequence[str],
    turn: int,
) -> AskDecision:
    if not fields:
        return Reject("ask_user needs at least one field")
    if len(set(fields)) != len(fields):
        return Reject("ask_user lists a field twice")
    gaps = missing(brief, kind)
    filled = [f for f in fields if f not in gaps]
    if filled:
        names = ", ".join(f.value for f in filled)
        return Reject(f"already known, do not ask: {names}")
    counts = ask_counts(brief)
    spent = [f for f in fields if f in brief.ask_exhausted or counts[f] >= MAX_ASKS_PER_FIELD]
    if spent:
        marked = [*brief.ask_exhausted, *[f for f in spent if f not in brief.ask_exhausted]]
        return Exhausted(
            brief=brief.model_copy(update={"ask_exhausted": marked}),
            spent=spent,
            still_needed=gaps,
        )
    askable = [f for f in gaps if f not in brief.ask_exhausted]
    left_out = [f for f in askable if f not in fields]
    if left_out:
        names = ", ".join(f.value for f in left_out)
        return Reject(f"ask for every missing field in one question; also ask: {names}")
    reasked = [f for f in fields if counts[f] >= 1]
    if reasked and not [o for o in options if o.strip()]:
        names = ", ".join(f.value for f in reasked)
        return Reject(f"{names} was asked before; a re-ask must offer quick-reply options")
    with_options = bool([o for o in options if o.strip()])
    records = [AskRecord(field=f, turn=turn, with_options=with_options) for f in fields]
    return Accept(brief=brief.model_copy(update={"asks": [*brief.asks, *records]}))
