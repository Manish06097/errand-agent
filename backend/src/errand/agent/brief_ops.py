"""Brief operations: what is required, what is missing, and the field-level merge (7.3).

The model proposes a patch through `update_brief`; code checks every value's evidence
against the user's current message, resolves time expressions, and merges field by
field, so a budget correction leaves area, window and service untouched with their
provenance intact. A patch with any rejected field changes nothing (10.7: a rejected
patch is a tool error that quotes the user's text back).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Literal

from errand.agent import timewin
from errand.domain.brief import (
    AreaFact,
    Brief,
    BriefField,
    BudgetFact,
    ErrandKind,
    Provenance,
    ServiceFact,
    TextFact,
    TimeWindowExpr,
    WindowFact,
)
from errand.world.contracts import Area, Service

F = BriefField

REQUIRED: Mapping[ErrandKind, tuple[BriefField, ...]] = {
    ErrandKind.BOOK: (F.SERVICE, F.AREA, F.WINDOW, F.BUDGET),
    ErrandKind.RESCHEDULE: (F.TARGET_BOOKING, F.NEW_WINDOW),
    ErrandKind.CANCEL: (F.TARGET_BOOKING,),
    ErrandKind.REBOOK: (F.TARGET_BOOKING, F.WINDOW),
}
# The fields update_brief can set in M3. Provider preference and booking targets come
# from history and memory (M9, M11), never from a bare model patch.
PATCHABLE: tuple[BriefField, ...] = (F.SERVICE, F.ISSUE, F.AREA, F.WINDOW, F.BUDGET)
MIN_EVIDENCE_CHARS = 4
ISSUE_MAX_CHARS = 80


def is_filled(brief: Brief, name: BriefField) -> bool:
    return getattr(brief, name.value) is not None


def missing(brief: Brief, kind: ErrandKind) -> list[BriefField]:
    """Required fields with no value, in REQUIRED order."""
    return [f for f in REQUIRED[kind] if not is_filled(brief, f)]


# ---------------------------------------------------------------- evidence


def normalise(text: str) -> str:
    """Case, whitespace and Unicode normalised, for evidence matching."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip()


@dataclass(frozen=True)
class EvidenceCheck:
    ok: bool
    reason: str | None = None


def check_evidence(quote: str, message: str, name: BriefField, value: object) -> EvidenceCheck:
    """Is `quote` real evidence for `name = value` in the user's current message?

    M3 checks the span: a normalised substring of the current message, at least 4
    characters. M8 adds the value-bound rules of 10.7 (the value must parse out of the
    span) here, in this one function, so every caller gets them at once. `name` and
    `value` are already passed for that.
    """
    q = normalise(quote)
    if len(q) < MIN_EVIDENCE_CHARS:
        return EvidenceCheck(False, f"the quote for {name.value} is shorter than 4 characters")
    if q not in normalise(message):
        return EvidenceCheck(False, f"the quote for {name.value} is not in the user's message")
    return EvidenceCheck(True)


# ---------------------------------------------------------------- patches


@dataclass(frozen=True)
class FieldValue:
    """One proposed value, before evidence and resolution."""

    name: BriefField
    value: Service | Area | str | int | Literal["none"] | TimeWindowExpr | None
    quote: str | None


@dataclass
class PatchOutcome:
    brief: Brief
    applied: bool
    set_fields: list[BriefField] = field(default_factory=list)
    cleared_fields: list[BriefField] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_patch(
    brief: Brief,
    *,
    sets: Sequence[FieldValue],
    clears: Sequence[FieldValue],
    message_text: str,
    message_id: str,
    turn: int,
    now: datetime,
    tz: tzinfo,
) -> PatchOutcome:
    """Check every value, then merge all or nothing."""
    errors: list[str] = []
    facts: dict[BriefField, ServiceFact | AreaFact | TextFact | WindowFact | BudgetFact | None] = {}
    tagged = [(item, False) for item in sets] + [(item, True) for item in clears]
    for item, is_clear in tagged:
        if item.name not in PATCHABLE:
            errors.append(f"{item.name.value} cannot be set with update_brief")
            continue
        if item.name in facts:
            errors.append(f"{item.name.value} appears twice in one patch")
            continue
        if item.quote is None:
            errors.append(f"no evidence quote for {item.name.value}")
            continue
        check = check_evidence(item.quote, message_text, item.name, item.value)
        if not check.ok:
            errors.append(check.reason or "evidence rejected")
            continue
        if is_clear:
            facts[item.name] = None
            continue
        prov = Provenance(source="user_msg", message_id=message_id, evidence=item.quote)
        fact, error = _fact(item, prov, turn, now, tz)
        if error is not None:
            errors.append(error)
            continue
        facts[item.name] = fact
    if errors:
        return PatchOutcome(brief=brief, applied=False, errors=errors)
    merged, set_fields, cleared = merge(brief, facts)
    return PatchOutcome(brief=merged, applied=True, set_fields=set_fields, cleared_fields=cleared)


def _fact(
    item: FieldValue, prov: Provenance, turn: int, now: datetime, tz: tzinfo
) -> tuple[ServiceFact | AreaFact | TextFact | WindowFact | BudgetFact | None, str | None]:
    v = item.value
    if item.name is F.SERVICE and isinstance(v, Service):
        return ServiceFact(value=v, prov=prov, set_at_turn=turn), None
    if item.name is F.AREA and isinstance(v, Area):
        return AreaFact(value=v, prov=prov, set_at_turn=turn), None
    if item.name is F.ISSUE and isinstance(v, str):
        text = v.strip()
        if not text or len(text) > ISSUE_MAX_CHARS:
            return None, f"issue must be 1 to {ISSUE_MAX_CHARS} characters"
        return TextFact(value=text, prov=prov, set_at_turn=turn), None
    if item.name is F.BUDGET and (v == "none" or (isinstance(v, int) and not isinstance(v, bool))):
        budget: int | Literal["none"] = "none" if v == "none" else int(v)
        if budget != "none" and budget <= 0:
            return None, "budget_max_inr must be a positive number of rupees, or none"
        return BudgetFact(value=budget, prov=prov, set_at_turn=turn), None
    if item.name is F.WINDOW and isinstance(v, TimeWindowExpr):
        resolved = timewin.resolve(v, now, tz)
        if isinstance(resolved, timewin.Unresolved):
            return None, f"window: {resolved.reason}; ask the user for the days they want"
        return WindowFact(value=resolved, expr=v, prov=prov, set_at_turn=turn), None
    return None, f"{item.name.value}: value of the wrong type"


def merge(
    brief: Brief,
    facts: Mapping[BriefField, ServiceFact | AreaFact | TextFact | WindowFact | BudgetFact | None],
) -> tuple[Brief, list[BriefField], list[BriefField]]:
    """Field-level merge. A field whose value does not change keeps its old fact."""
    update: dict[str, object] = {}
    set_fields: list[BriefField] = []
    cleared: list[BriefField] = []
    for name, fact in facts.items():
        current = getattr(brief, name.value)
        if fact is None:
            if current is not None:
                update[name.value] = None
                cleared.append(name)
            continue
        if current is not None and _same_value(current.value, fact.value):
            continue
        update[name.value] = fact
        set_fields.append(name)
    if not update:
        return brief, [], []
    update["version"] = brief.version + 1
    return brief.model_copy(update=update), set_fields, cleared


def _same_value(a: object, b: object) -> bool:
    if hasattr(a, "model_dump") and hasattr(b, "model_dump"):
        return bool(a.model_dump() == b.model_dump())
    return a == b


def known_lines(brief: Brief) -> list[str]:
    """`field: value (from "...")` for every filled field, evidence cut to 60 chars."""
    lines: list[str] = []
    for name in BriefField:
        fact = getattr(brief, name.value)
        if fact is None:
            continue
        lines.append(f"{name.value}: {render_value(fact)}{_source(fact.prov)}")
    return lines


def render_value(fact: object) -> str:
    value = getattr(fact, "value", None)
    label = getattr(value, "label", None)
    if isinstance(label, str):
        return label
    if isinstance(value, Service | Area):
        return value.value
    return str(value)


def _source(prov: Provenance) -> str:
    if prov.evidence is None:
        return f" (from {prov.source})"
    quote = prov.evidence if len(prov.evidence) <= 60 else prov.evidence[:57] + "..."
    return f' (from the user: "{quote}")'
