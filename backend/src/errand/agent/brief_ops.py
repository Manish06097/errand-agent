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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Literal

from errand.agent import timewin
from errand.domain import aliases, evidence
from errand.domain.brief import (
    AreaFact,
    Brief,
    BriefField,
    BudgetFact,
    ErrandKind,
    Provenance,
    ProviderPref,
    ProviderPrefFact,
    ServiceFact,
    TextFact,
    TimeWindowExpr,
    WindowFact,
)
from errand.domain.errand import Errand
from errand.domain.windows import slot_fits
from errand.world.contracts import Area, BookingItem, Service

F = BriefField

REQUIRED: Mapping[ErrandKind, tuple[BriefField, ...]] = {
    ErrandKind.BOOK: (F.SERVICE, F.AREA, F.WINDOW, F.BUDGET),
    # The new day is the brief's `window` (N1, 9.9): one path for dates, so update_brief's
    # evidence check and timewin apply to a reschedule exactly as to a booking.
    ErrandKind.RESCHEDULE: (F.TARGET_BOOKING, F.WINDOW),
    ErrandKind.CANCEL: (F.TARGET_BOOKING,),
    ErrandKind.REBOOK: (F.TARGET_BOOKING, F.SERVICE, F.AREA, F.WINDOW),
}
# The fields update_brief can set in M3. Provider preference and booking targets come
# from history and memory (M9, M11), never from a bare model patch.
PATCHABLE: tuple[BriefField, ...] = (F.SERVICE, F.ISSUE, F.AREA, F.WINDOW, F.BUDGET)
MIN_EVIDENCE_CHARS = 4
ISSUE_MAX_CHARS = 80


def is_filled(brief: Brief, name: BriefField) -> bool:
    return getattr(brief, name.value) is not None


def pinned(brief: Brief) -> str | None:
    """The provider id the user chose by history ("the same electrician as last time"), if
    any: the user has chosen the provider, not asked for a comparison (7.3)."""
    pref = brief.provider_pref
    if pref is not None and pref.prov.source == "booking_history":
        return pref.value.prefer_id
    return None


def missing(brief: Brief, kind: ErrandKind) -> list[BriefField]:
    """Required fields with no value, in REQUIRED order. A pinned provider needs no budget:
    the card shows the held quote next to the last price, and the click approves that
    exact price (a budget is the user's constraint; none is invented from the last one)."""
    gaps = [f for f in REQUIRED[kind] if not is_filled(brief, f)]
    if kind is ErrandKind.BOOK and pinned(brief) is not None:
        gaps = [f for f in gaps if f is not F.BUDGET]
    return gaps


# ---------------------------------------------------------------- evidence


def normalise(text: str) -> str:
    """Case, whitespace and Unicode normalised, for evidence matching."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip()


@dataclass(frozen=True)
class EvidenceCheck:
    ok: bool
    reason: str | None = None


def check_evidence(
    quote: str,
    message: str,
    name: BriefField,
    value: object,
    more_quotes: Sequence[str] = (),
    earlier: Sequence[str] = (),
) -> EvidenceCheck:
    """Is the quote real evidence for `name = value` in the user's current message?

    Two layers (10.7). The span: every quote is a normalised substring of the current
    message, at least 4 characters. The value: what the spans say must give the value,
    so "under" (a substring of "under ₹1,500") cannot back a budget of 5,000:
    - budget: a number parses out of the spans and equals the value; "none" needs a
      no-limit phrase;
    - window: the phrase is in the spans, a `part_of_day` the patch sets has a day-part
      word in them (morning, afternoon, evening, or "any time" for "any"), and
      `not_before` / `not_after` each parse to the time given (a bare "5" reads as 05:00
      or 17:00, either may be the value);
    - area, service: an alias of that enum value is in the spans;
    - issue: every content word of the value is in the spans.
    A clear (`value` None) needs the span only. `more_quotes` are further spans for the
    same field (a window phrase and its "not before 10" are often two pieces of one
    sentence); each must pass the span rule, and the value rules read all of them.

    `earlier` are the user's earlier messages of the same errand (9.9). Only a window's
    `not_before` / `not_after` may cite them: "not before 10 a.m." said before the day was
    known cannot be stored (a window needs a day phrase), and the user will not say it
    twice. The window phrase and the day part still have to come from the current message,
    so a quote that sits only in an earlier message is refused for everything else.
    """
    spans: list[str] = []
    bounds: list[str] = []
    haystack = normalise(message)
    use_earlier = name is F.WINDOW and value is not None
    earlier_text = [normalise(m) for m in earlier] if use_earlier else []
    for raw in (quote, *more_quotes):
        q = normalise(raw)
        if len(q) < MIN_EVIDENCE_CHARS:
            return EvidenceCheck(False, f"the quote for {name.value} is shorter than 4 characters")
        if q in haystack:
            spans.append(q)
        elif any(q in text for text in earlier_text):
            bounds.append(q)
        else:
            return EvidenceCheck(False, f"the quote for {name.value} is not in the user's message")
    if value is None:
        return EvidenceCheck(True)
    reason = _value_problem(name, value, " ; ".join(spans), " ; ".join(bounds))
    return EvidenceCheck(False, reason) if reason else EvidenceCheck(True)


def _value_problem(
    name: BriefField, value: object, span: str, earlier_span: str = ""
) -> str | None:
    """Why the spans do not give the value, or None when they do. `earlier_span` holds
    quotes from earlier messages; only a window's time bounds read it."""
    if name is F.BUDGET:
        if value == "none":
            if not evidence.has_no_limit(span):
                return f"budget: {span!r} does not say there is no limit"
        elif isinstance(value, int) and value not in evidence.parse_amounts(span):
            return f"budget: {span!r} does not give an amount of ₹{value:,}"
        return None
    if name is F.WINDOW and isinstance(value, TimeWindowExpr):
        if normalise(value.phrase) not in span:
            return f"window: {span!r} does not contain the phrase {value.phrase!r}"
        # A day part the patch sets needs its own word; one carried over from the old
        # window (carry_over) is not in the patch, so it is not checked here.
        if value.part_of_day is not None and value.part_of_day not in evidence.day_parts(span):
            return f"window: {span!r} does not say {value.part_of_day}; a day part needs its words"
        times = evidence.parse_times(f"{span} ; {earlier_span}" if earlier_span else span)
        for label, wanted in (("not_before", value.not_before), ("not_after", value.not_after)):
            if wanted is not None and wanted not in times:
                return f"window: {span!r} does not give {label} {wanted:%H:%M}"
        return None
    if name in (F.AREA, F.SERVICE) and isinstance(value, Service | Area):
        if not any(a in span for a in aliases.aliases_of(name.value, value.value)):
            return f"{name.value}: {span!r} does not name {value.value.replace('_', ' ')}"
        return None
    if name is F.ISSUE and isinstance(value, str):
        if not evidence.words_in(normalise(value), span):
            return f"issue: {span!r} does not contain the words of {value!r}"
        return None
    return None


# ---------------------------------------------------------------- patches


@dataclass(frozen=True)
class FieldValue:
    """One proposed value, before evidence and resolution."""

    name: BriefField
    value: Service | Area | str | int | Literal["none"] | TimeWindowExpr | None
    quote: str | None
    more_quotes: tuple[str, ...] = ()


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
    earlier_messages: Sequence[str] = (),
) -> PatchOutcome:
    """Check every value, then merge all or nothing. `earlier_messages` are the user's
    earlier messages of this errand (see `check_evidence`)."""
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
        check = check_evidence(
            item.quote, message_text, item.name, item.value, item.more_quotes, earlier_messages
        )
        if not check.ok:
            errors.append(check.reason or "evidence rejected")
            continue
        if is_clear:
            facts[item.name] = None
            continue
        prov = Provenance(source="user_msg", message_id=message_id, evidence=item.quote)
        fact, error = _fact(item, prov, turn, now, tz, brief.window)
        if error is not None:
            errors.append(error)
            continue
        facts[item.name] = fact
    if errors:
        return PatchOutcome(brief=brief, applied=False, errors=errors)
    merged, set_fields, cleared = merge(brief, facts)
    return PatchOutcome(brief=merged, applied=True, set_fields=set_fields, cleared_fields=cleared)


def carry_over(new: TimeWindowExpr, old: TimeWindowExpr | None) -> TimeWindowExpr:
    """A window set over an existing one keeps what the new expression leaves unset.

    "Actually Thursday or Friday" after "mornings only" names days and nothing else, so
    the day part and time bounds the user already gave still hold (7.3, 10.5). Only a
    sub-field the patch leaves null is filled. "any" is the explicit way to widen: it drops
    the carried day part and the carried time bounds, and it needs its own evidence (10.7).
    The merged expression is what the brief stores, so a chain of corrections keeps carrying.
    """
    if old is None:
        return new
    if new.part_of_day == "any":
        # "any time" widens: the bounds kept from earlier go too. Bounds this patch sets
        # itself still apply.
        return new
    return new.model_copy(
        update={
            "part_of_day": new.part_of_day if new.part_of_day is not None else old.part_of_day,
            "not_before": new.not_before if new.not_before is not None else old.not_before,
            "not_after": new.not_after if new.not_after is not None else old.not_after,
        }
    )


def _empty_window_error(said: TimeWindowExpr, kept: TimeWindowExpr) -> str:
    """The daily window is empty: name the conflict, which is hours and not days."""
    carried = [
        f"{label} {bound:%H:%M}"
        for label, bound, own in (
            ("not before", kept.not_before, said.not_before),
            ("not after", kept.not_after, said.not_after),
        )
        if bound is not None and own is None
    ]
    if not carried:
        return "window: the daily window is empty; ask the user which hours they want"
    part = said.part_of_day or kept.part_of_day
    return (
        f"window: {part or 'the day part'} cannot fit {' and '.join(carried)} kept from "
        f"earlier; ask whether they want {part or 'that day part'} or {' and '.join(carried)}"
    )


def _fact(
    item: FieldValue,
    prov: Provenance,
    turn: int,
    now: datetime,
    tz: tzinfo,
    current_window: WindowFact | None = None,
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
        expr = carry_over(v, current_window.expr if current_window else None)
        resolved = timewin.resolve(expr, now, tz)
        if isinstance(resolved, timewin.Unresolved):
            if resolved.empty_daily:
                return None, _empty_window_error(v, expr)
            return None, f"window: {resolved.reason}; ask the user for the days they want"
        return WindowFact(value=resolved, expr=expr, prov=prov, set_at_turn=turn), None
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
    changed_service = F.SERVICE in set_fields and brief.service is not None
    if changed_service and brief.issue is not None and F.ISSUE not in facts:
        # A different service makes the old issue ("leaking tap") meaningless: it goes in
        # the same merge, so the brief moves once (7.3, INVALIDATES).
        update["issue"] = None
        cleared.append(F.ISSUE)
    update["version"] = brief.version + 1
    return brief.model_copy(update=update), set_fields, cleared


def _same_value(a: object, b: object) -> bool:
    if hasattr(a, "model_dump") and hasattr(b, "model_dump"):
        return bool(a.model_dump() == b.model_dump())
    return a == b


# ---------------------------------------------------------------- invalidation (7.3, 8.5)

What = Literal["shortlist", "hold", "approval", "issue", "ranking"]
Condition = Literal["held_slot_outside_window", "quote_over_budget", "provider_avoided"]


@dataclass(frozen=True)
class Rule:
    """What a change to one field discards downstream.

    `always` is discarded whenever the field's value changes. `when` is discarded only if
    `condition` holds for the live hold and card. The shortlist is never re-filtered in
    place: its slots were filtered by the old window and its candidates by the old
    budget, so it is marked stale (the brief version moves, which stops `get_provider`
    honouring it) and the planner's next `search_providers` rebuilds it.
    """

    always: tuple[What, ...]
    when: tuple[What, ...] = ()
    condition: Condition | None = None


INVALIDATES: Mapping[BriefField, Rule] = {
    F.SERVICE: Rule(always=("shortlist", "hold", "approval", "issue")),
    F.AREA: Rule(always=("shortlist", "hold", "approval")),
    F.WINDOW: Rule(
        always=("shortlist",), when=("hold", "approval"), condition="held_slot_outside_window"
    ),
    F.BUDGET: Rule(always=("ranking",), when=("approval",), condition="quote_over_budget"),
    F.PROVIDER_PREF: Rule(always=("ranking",), when=("approval",), condition="provider_avoided"),
    F.TARGET_BOOKING: Rule(always=("shortlist", "hold", "approval")),
}
# What the user is told ("This card is out of date: you changed the day").
CHANGED_WORDS: Mapping[BriefField, str] = {
    F.SERVICE: "the service",
    F.AREA: "the area",
    F.WINDOW: "the day or time",
    F.BUDGET: "the budget",
    F.PROVIDER_PREF: "the provider",
    F.TARGET_BOOKING: "the booking",
}


@dataclass(frozen=True)
class Invalidation:
    """One thing a change discards: the `invalidate {what, id, reason}` trace record."""

    what: What
    id: str
    reason: str
    field: BriefField


def _condition_holds(cond: Condition, new_brief: Brief, errand: Errand, tz: tzinfo) -> bool:
    hold = errand.hold
    if cond == "held_slot_outside_window":
        if new_brief.window is None:
            return True  # a cleared window admits no slot
        if hold is None:
            return errand.approval_id is not None  # a card with no hold cannot be shown to fit
        return not slot_fits(hold.slot, new_brief.window.value, tz)
    if hold is None:
        return False
    if cond == "quote_over_budget":
        budget = new_brief.budget_max_inr
        return (
            budget is not None
            and isinstance(budget.value, int)
            and (hold.quoted_price_inr > budget.value)
        )
    pref = new_brief.provider_pref
    return pref is not None and hold.provider_id in pref.value.avoid_ids


def _exists(what: What, old_brief: Brief, new_brief: Brief, errand: Errand) -> str | None:
    """The id of the thing a rule would discard, or None if there is nothing to discard."""
    if what in ("shortlist", "ranking"):
        sl = errand.shortlist
        return None if sl is None else f"shortlist_v{sl.brief_version}"
    if what == "hold":
        return errand.hold.hold_id if errand.hold is not None else None
    if what == "approval":
        return errand.approval_id
    # the merge cleared it: there was an issue and now there is none
    return "issue" if old_brief.issue is not None and new_brief.issue is None else None


def invalidations(
    field: BriefField, old_brief: Brief, new_brief: Brief, errand: Errand, tz: tzinfo
) -> list[Invalidation]:
    """What the change of `field` from `old_brief` to `new_brief` discards. Pure.

    `errand` is the errand before the change (its shortlist, hold and card). A field
    whose value did not change discards nothing; a field with no row (the free-text
    ones) neither. M11's forget and correct call this too.
    """
    rule = INVALIDATES.get(field)
    old, new = getattr(old_brief, field.value), getattr(new_brief, field.value)
    if rule is None or (old is not None and new is not None and _same_value(old.value, new.value)):
        return []
    if old is None and new is None:
        return []
    reason = f"{field.value}_changed"
    kinds: list[tuple[What, str]] = [(w, reason) for w in rule.always]
    if rule.condition is not None and _condition_holds(rule.condition, new_brief, errand, tz):
        why = {"quote_over_budget": "quote_over_budget", "provider_avoided": "provider_avoided"}
        kinds += [(w, why.get(rule.condition, reason)) for w in rule.when]
    out: list[Invalidation] = []
    for what, why_code in kinds:
        ident = _exists(what, old_brief, new_brief, errand)
        if ident is not None:
            out.append(Invalidation(what, ident, why_code, field))
    return out


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
    if isinstance(value, ProviderPref):
        prov = getattr(fact, "prov", None)
        if prov is not None and prov.source == "memory":
            # A saved preference ranks and filters in code; it is not "the same as last time".
            parts = ([f"prefers {value.prefer_id}"] if value.prefer_id else []) + (
                [f"avoids {', '.join(value.avoid_ids)}"] if value.avoid_ids else []
            )
            return "a saved preference: " + "; ".join(parts)
        return f"the same provider as last time ({value.prefer_id})"
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


# ---------------------------------------------------------------- history (7.3)

_LAST = r"(?:last|previous|earlier|before)"
HISTORY_REFERENCE = re.compile(
    rf"\blast time\b|\bagain\b|\bsame\b[^.?!]{{0,40}}\b(?:as|like)\b[^.?!]{{0,15}}\b{_LAST}\b"
    rf"|\bsame\b[^.?!]{{0,20}}\b{_LAST}\b",
    re.I,
)


@dataclass
class PinOutcome:
    brief: Brief
    applied: bool
    set_fields: list[BriefField] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def pin_from_history(
    brief: Brief,
    *,
    booking: BookingItem,
    provider_area: Area,
    quote: str | None,
    message_text: str,
    message_id: str,
    turn: int,
    budget_source: Callable[[Service], BudgetFact | None] | None = None,
) -> PinOutcome:
    """Pin the provider of one of the user's own bookings ("the same electrician as last
    time"), by code (7.3, 10.7).

    The evidence is the user's words: a span of the current message that carries a history
    reference ("last time", "again", "the same ... as last time") or the provider's name.
    The booking comes from the planner's `list_bookings` view of this run, resolved by
    code from its handle. Then code applies 7.3: the service is the booking's, the area is
    the provider's, `provider_pref` names the provider, and the budget stays unasked.
    `budget_source` is where a remembered ceiling for the service plugs in (N2); with none
    (the default) no budget is invented.
    """
    if quote is None:
        return PinOutcome(brief, False, errors=["no evidence quote for provider_pref"])
    check = check_evidence(quote, message_text, F.PROVIDER_PREF, None)
    if not check.ok:
        return PinOutcome(brief, False, errors=[check.reason or "evidence rejected"])
    names_provider = normalise(booking.provider_name) in normalise(quote)
    if not (HISTORY_REFERENCE.search(quote) or names_provider):
        return PinOutcome(
            brief,
            False,
            errors=[
                "the quote for provider_pref carries no reference to a past booking "
                '("last time", "again", "the same ... as before") and does not name the provider'
            ],
        )
    if brief.service is not None and brief.service.value != booking.service:
        return PinOutcome(
            brief,
            False,
            errors=[
                f"that booking is {booking.service.value}, but the request is for "
                f"{brief.service.value.value}"
            ],
        )
    prov = Provenance(
        source="booking_history",
        message_id=message_id,
        booking_id=booking.booking_id,
        evidence=quote,
    )
    facts: dict[str, ServiceFact | AreaFact | ProviderPrefFact | BudgetFact] = {
        "service": ServiceFact(value=booking.service, prov=prov, set_at_turn=turn),
        "area": AreaFact(value=provider_area, prov=prov, set_at_turn=turn),
        "provider_pref": ProviderPrefFact(
            value=ProviderPref(prefer_id=booking.provider_id), prov=prov, set_at_turn=turn
        ),
    }
    if budget_source is not None and brief.budget_max_inr is None:
        remembered = budget_source(booking.service)
        if remembered is not None:
            facts["budget_max_inr"] = remembered
    changed = [
        F(name)
        for name, fact in facts.items()
        if getattr(brief, name) is None or getattr(brief, name).value != fact.value
    ]
    if not changed:
        return PinOutcome(brief, True)
    pinned_brief = brief.model_copy(update={**facts, "version": brief.version + 1})
    return PinOutcome(pinned_brief, True, set_fields=changed)
