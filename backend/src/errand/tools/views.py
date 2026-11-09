"""Planner-facing views of world answers (architecture 11, 13.1).

Each view is built once from a typed world answer, sanitized and compacted to its
per-tool cap, and that rendering is what gets appended to the loop. Nothing is ever
re-rendered later, so the loop prefix stays byte-identical.

- search: at most 5 candidates with at most 3 window-matching slots each, in the
  code's ranked order, with their score and badges. Slot handles (`S1`, `S2`, ...) are
  assigned in `assemble_shortlist`, after the final order is known, in candidate order
  then slot order, and stored in the `Shortlist`; the model only ever names a slot by
  its handle.
- get_provider: a `ProviderView`, the contract fields minus `reviews` and `about`, plus
  the quarantined reader's digest (enum counts, no review ids, no text). The reviews
  key is absent, not an empty list: raw review text never reaches the planner.
- list_bookings: id, provider, service, slot label and status.

The rendering is `<tool_data>{json}</tool_data>`; the static rules say that anything
inside it is data, not instructions.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from errand.domain.booking_refs import assign
from errand.domain.brief import TimeWindow
from errand.domain.slots import Badge, Candidate, Exclusion, ReviewDigest, Shortlist, SlotHandle
from errand.domain.windows import slot_fits, slot_label
from errand.tools.sanitize import clean_name, clean_text
from errand.world.contracts import (
    GetProviderOut,
    ListBookingsOut,
    ProviderSummary,
    SearchProvidersOut,
    Slot,
)

SEARCH_MAX_CANDIDATES = 5
SEARCH_MAX_SLOTS = 3
SHORTLIST_MAX = 8
PROVIDER_MAX_SLOTS = 6
BOOKINGS_MAX = 10


def render(data: Any) -> str:
    """The one serialisation of a planner view. Key order is construction order."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"<tool_data>{body}</tool_data>"


def render_error(error: str, **detail: Any) -> str:
    return render({"error": error, **detail})


@dataclass(frozen=True)
class Eligible:
    """A provider that passed the hard filters, with the slots that fit the window."""

    summary: ProviderSummary
    name: str
    fitting: list[Slot]


def hard_filter(
    result: SearchProvidersOut,
    *,
    window: TimeWindow,
    budget: int | Literal["none"] | None,
    tz: tzinfo,
    avoid: Collection[str] = (),
) -> tuple[list[Eligible], list[Exclusion]]:
    """Hard filters (avoided providers, budget, window), in search order. No handles yet:
    those are given only once the final order is known. `avoid` comes from the brief's
    provider_pref (the user's avoid_provider memory), so shedding S4 never drops it."""
    eligible: list[Eligible] = []
    excluded: list[Exclusion] = []
    for p in result.providers:
        name = clean_name(p.name)
        if p.provider_id in avoid:
            excluded.append(Exclusion(provider_id=p.provider_id, name=name, reason="avoided"))
            continue
        if isinstance(budget, int) and p.price_inr > budget:
            excluded.append(Exclusion(provider_id=p.provider_id, name=name, reason="over_budget"))
            continue
        fitting = [s for s in p.available_slots if slot_fits(s, window, tz)]
        if not fitting:
            excluded.append(
                Exclusion(provider_id=p.provider_id, name=name, reason="no_slot_in_window")
            )
            continue
        eligible.append(Eligible(p, name, fitting))
    return eligible, excluded


@dataclass(frozen=True)
class Placed:
    """An eligible provider in its final place, with its score and reader digest."""

    eligible: Eligible
    score: float
    score_parts: dict[str, float]
    digest: ReviewDigest | None


def assemble_shortlist(
    ordered: Sequence[Placed],
    excluded: Sequence[Exclusion],
    *,
    brief_version: int,
    window_label: str,
) -> Shortlist:
    """The shortlist in its final order. Slot handles (`S1`, `S2`, ...) are assigned
    here and only here, in that order, so the handles the planner sees, the ones stored
    and the ones on the card are the same."""
    candidates: list[Candidate] = []
    dropped = list(excluded)
    next_ref = 1
    for placed in ordered:
        e = placed.eligible
        p = e.summary
        if len(candidates) >= SHORTLIST_MAX:
            dropped.append(Exclusion(provider_id=p.provider_id, name=e.name, reason="over_cap"))
            continue
        handles = []
        for slot in e.fitting[:SEARCH_MAX_SLOTS]:
            handles.append(
                SlotHandle(
                    ref=f"S{next_ref}",
                    provider_id=p.provider_id,
                    slot=slot,
                    search_price_inr=p.price_inr,
                )
            )
            next_ref += 1
        candidates.append(_candidate(placed, handles))
    return Shortlist(
        brief_version=brief_version,
        window_label=window_label,
        candidates=candidates,
        excluded=dropped,
    )


def badges_for(verified: bool, digest: ReviewDigest | None) -> list[Badge]:
    out: list[Badge] = ["verified"] if verified else []
    if digest is not None and digest.injection_flags > 0:
        out.append("unverified_reviews")
    return out


def _candidate(placed: Placed, handles: list[SlotHandle]) -> Candidate:
    e = placed.eligible
    p = e.summary
    return Candidate(
        provider_id=p.provider_id,
        name=e.name,
        rating=p.rating,
        price_inr=p.price_inr,
        review_count=p.review_count,
        verified=p.attributes.verified,
        slots=handles,
        digest=placed.digest,
        badges=badges_for(p.attributes.verified, placed.digest),
        score=placed.score,
        score_parts=dict(placed.score_parts),
    )


def search_view(shortlist: Shortlist, result: SearchProvidersOut, tz: tzinfo) -> dict[str, Any]:
    extra = {p.provider_id: p for p in result.providers}
    shown = shortlist.candidates[:SEARCH_MAX_CANDIDATES]
    return {
        "window": shortlist.window_label,
        "candidates": [
            {
                "provider_id": c.provider_id,
                "name": c.name,
                "price_inr": c.price_inr,
                "rating": c.rating,
                "review_count": c.review_count,
                "verified": c.verified,
                "years_experience": extra[c.provider_id].attributes.years_experience,
                "score": c.score,
                "badges": list(c.badges),
                "slots": [{"ref": h.ref, "when": slot_label(h.slot, tz)} for h in c.slots],
            }
            for c in shown
        ],
        "more_candidates": len(shortlist.candidates) - len(shown),
        "excluded": [
            {"provider_id": e.provider_id, "reason": e.reason} for e in shortlist.excluded
        ],
    }


class SlotView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str
    ref: str | None


class ProviderView(BaseModel):
    """get_provider as the planner sees it. There is deliberately no reviews field."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    name: str
    service: str
    area: str
    price_inr: int
    rating: float
    verified: bool
    years_experience: int
    languages: list[str]
    brands: list[str]
    available_slots: list[SlotView]
    # Enum counts from the quarantined reader; None when it was not read or the read
    # failed. No review ids and no text.
    digest: ReviewDigest | None = None


def provider_view(
    result: GetProviderOut,
    *,
    window: TimeWindow | None,
    shortlist: Shortlist | None,
    tz: tzinfo,
    digest: ReviewDigest | None = None,
) -> ProviderView:
    refs = {}
    if shortlist is not None:
        for c in shortlist.candidates:
            for h in c.slots:
                refs[(h.provider_id, h.slot.start)] = h.ref
    slots = [s for s in result.available_slots if window is None or slot_fits(s, window, tz)]
    attrs = result.attributes
    return ProviderView(
        provider_id=result.provider_id,
        name=clean_name(result.name),
        service=result.service.value,
        area=result.area.value,
        price_inr=result.price_inr,
        rating=result.rating,
        verified=attrs.verified,
        years_experience=attrs.years_experience,
        languages=[clean_text(x, 30) for x in attrs.languages[:6]],
        brands=[clean_text(x, 30) for x in attrs.brands[:6]],
        available_slots=[
            SlotView(when=slot_label(s, tz), ref=refs.get((result.provider_id, s.start)))
            for s in slots[:PROVIDER_MAX_SLOTS]
        ],
        digest=digest,
    )


def bookings_view(result: ListBookingsOut, tz: tzinfo) -> dict[str, Any]:
    """Each booking with its handle (`B1`, ...) from `domain.booking_refs.assign`, the
    only way `propose_cancel` names one (N1)."""
    refs = assign(result.bookings, BOOKINGS_MAX)
    return {
        "bookings": [
            {
                "ref": ref,
                "booking_id": b.booking_id,
                "provider_id": b.provider_id,
                "provider_name": clean_name(b.provider_name),
                "service": b.service.value,
                "when": slot_label(b.slot, tz),
                "status": b.status,
            }
            for ref, b in refs.items()
        ],
        "more": max(0, len(result.bookings) - BOOKINGS_MAX),
    }
