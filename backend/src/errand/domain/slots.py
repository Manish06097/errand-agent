"""Slot handles, candidates, the shortlist and hold references (7.1).

The model names a slot only by its handle (`S1`, `S2`, ...). Code assigns handles in
candidate order, then slot order, so the same search gives the same handles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.world.contracts import Slot


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotHandle(_Model):
    ref: str
    provider_id: str
    slot: Slot
    search_price_inr: int


class ReviewDigest(_Model):
    """Filled by the quarantined reader in M10; until then a candidate has none."""

    n_reviews: int
    sentiment_counts: dict[str, int] = Field(default_factory=dict)
    top_themes: list[str] = Field(default_factory=list)
    injection_flags: int = 0
    reader_model: str


Badge = Literal["verified", "unverified_reviews", "cheapest", "top_rated"]


class Candidate(_Model):
    provider_id: str
    name: str
    rating: float
    price_inr: int
    review_count: int = 0
    verified: bool = False
    slots: list[SlotHandle]  # already filtered by the window
    digest: ReviewDigest | None = None
    badges: list[Badge] = Field(default_factory=list)
    # Ranking lands in M8; until then the score is 0 and candidates keep search order.
    score: float = 0.0
    score_parts: dict[str, float] = Field(default_factory=dict)


class Exclusion(_Model):
    provider_id: str
    name: str
    reason: str


class Shortlist(_Model):
    brief_version: int
    window_label: str
    candidates: list[Candidate] = Field(max_length=8)
    excluded: list[Exclusion] = Field(default_factory=list)

    def handle(self, ref: str) -> SlotHandle | None:
        for candidate in self.candidates:
            for handle in candidate.slots:
                if handle.ref == ref:
                    return handle
        return None

    def provider_ids(self) -> list[str]:
        return [c.provider_id for c in self.candidates]


class HoldRef(_Model):
    """Set by `prepare_proposal` in M4."""

    hold_record_id: str
    hold_id: str
    provider_id: str
    slot: Slot
    search_price_inr: int
    quoted_price_inr: int
    expires_at: datetime
