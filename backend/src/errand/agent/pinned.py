"""A pinned provider's slot handles (7.3, 10.2, N1).

"The same electrician as last time" names a provider that search may never return (E4 is
not in the default search results). The planner opens it with `get_provider`, and code
turns the slots that fit the window into handles the same way a search does, so that
`propose_booking(provider_id, slot_ref)` can name one. The handles join the errand's
shortlist for the current brief version; an existing one keeps its own and the numbering
continues after it. Nothing here calls the world or the model.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.domain.brief import TimeWindow
from errand.domain.slots import Candidate, ReviewDigest, Shortlist, SlotHandle
from errand.domain.windows import slot_fits
from errand.tools.sanitize import clean_name
from errand.tools.views import badges_for
from errand.world.contracts import GetProviderOut

MAX_SLOTS = 6


def with_pinned(
    current: Shortlist | None,
    result: GetProviderOut,
    *,
    brief_version: int,
    window: TimeWindow | None,
    digest: ReviewDigest | None,
    now: datetime,
    tz: tzinfo,
) -> Shortlist:
    """`current` (if it belongs to this brief version) plus the provider's fitting slots."""
    base = current if current is not None and current.brief_version == brief_version else None
    kept = [c for c in (base.candidates if base else []) if c.provider_id != result.provider_id]
    next_ref = 1 + sum(len(c.slots) for c in kept)
    handles: list[SlotHandle] = []
    for slot in result.available_slots:
        if slot.start <= now or (window is not None and not slot_fits(slot, window, tz)):
            continue
        if len(handles) >= MAX_SLOTS:
            break
        handles.append(
            SlotHandle(
                ref=f"S{next_ref + len(handles)}",
                provider_id=result.provider_id,
                slot=slot,
                search_price_inr=result.price_inr,
            )
        )
    candidate = Candidate(
        provider_id=result.provider_id,
        name=clean_name(result.name),
        rating=result.rating,
        price_inr=result.price_inr,
        review_count=len(result.reviews),
        verified=result.attributes.verified,
        slots=handles,
        digest=digest,
        badges=badges_for(result.attributes.verified, digest),
    )
    return Shortlist(
        brief_version=brief_version,
        window_label=window.label if window is not None else "any time",
        candidates=[*kept, candidate],
        excluded=list(base.excluded) if base else [],
    )
