"""Code ranks the shortlist (architecture 10.6, ADR 0015). A pure function.

    score = 0.35 rating_norm + 0.25 price_headroom + 0.15 slot_fit
          + 0.15 review_signal + 0.10 pref_match

Each part is in [0, 1]:
- rating_norm: (rating - 1) / 4, the provider's listed star rating;
- price_headroom: with a numeric budget, (budget - price) / budget, floored at 0; with
  no budget ("none" or not set), cheapest price / price, so the cheapest gets 1;
- slot_fit: how many slots fit the window, up to 3, over 3 (more choice is better);
- review_signal: the mean of (rating - 1) / 4 over the reviews the quarantined reader
  positively labelled clean. It is 0 if any review was flagged, if the provider was
  never read, or if the read failed. A read in which any review that was sent came
  back unlabelled counts as failed (the reader fails it closed as incomplete_labels;
  the check is repeated here), so no review is ever silently left out of the mean.
  Reader sentiment never enters the score. So a review can lower its provider's rank (by
  being flagged) but never raise it past what an honest review with the same stars
  would;
- pref_match: 0 until memory (M11).

Ties go to the smaller provider_id, so the same inputs always give the same order.
Parts and score are rounded to 4 places so the order never hangs on float noise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

WEIGHTS: Mapping[str, float] = {
    "rating": 0.35,
    "price_headroom": 0.25,
    "slot_fit": 0.15,
    "review_signal": 0.15,
    "pref_match": 0.10,
}
SLOT_FIT_FULL = 3
PLACES = 4

Budget = int | Literal["none"] | None


@dataclass(frozen=True)
class RankInput:
    provider_id: str
    rating: float
    price_inr: int
    fitting_slots: int
    # review_id -> star rating of the reviews the reader was given
    review_ratings: Mapping[str, int] = field(default_factory=dict)
    # review_id -> injection_suspected for every labelled review; None if not read
    review_flags: Mapping[str, bool] | None = None
    pref_match: float = 0.0


@dataclass(frozen=True)
class Ranked:
    provider_id: str
    score: float
    parts: dict[str, float]


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


def review_signal(ratings: Mapping[str, int], flags: Mapping[str, bool] | None) -> float:
    if flags is None or any(flags.values()) or any(rid not in flags for rid in ratings):
        return 0.0
    clean = [ratings[rid] for rid, flagged in flags.items() if not flagged and rid in ratings]
    if not clean:
        return 0.0
    return _clamp(sum((r - 1) / 4 for r in clean) / len(clean))


def score_parts(c: RankInput, *, budget: Budget, cheapest: int) -> dict[str, float]:
    if isinstance(budget, int) and budget > 0:
        headroom = _clamp((budget - c.price_inr) / budget)
    else:
        headroom = _clamp(cheapest / c.price_inr) if c.price_inr > 0 else 1.0
    parts = {
        "rating": _clamp((c.rating - 1) / 4),
        "price_headroom": headroom,
        "slot_fit": _clamp(min(c.fitting_slots, SLOT_FIT_FULL) / SLOT_FIT_FULL),
        "review_signal": review_signal(c.review_ratings, c.review_flags),
        "pref_match": _clamp(c.pref_match),
    }
    return {k: round(v, PLACES) for k, v in parts.items()}


def rank(candidates: Sequence[RankInput], *, budget: Budget) -> list[Ranked]:
    """Every candidate, best first; ties by provider_id."""
    if not candidates:
        return []
    cheapest = min(c.price_inr for c in candidates)
    ranked = []
    for c in candidates:
        parts = score_parts(c, budget=budget, cheapest=cheapest)
        score = round(sum(WEIGHTS[k] * v for k, v in parts.items()), PLACES)
        ranked.append(Ranked(c.provider_id, score, parts))
    return sorted(ranked, key=lambda r: (-r.score, r.provider_id))
