"""Slot handles are assigned after the final order, never before (10.6)."""

from __future__ import annotations

from datetime import datetime, timedelta

from errand.clock import IST
from errand.domain.slots import ReviewDigest
from errand.tools.views import Eligible, Placed, assemble_shortlist
from errand.world.contracts import Area, Attributes, ProviderSummary, Service, Slot


def _eligible(pid: str, n_slots: int) -> Eligible:
    start = datetime(2026, 11, 10, 9, tzinfo=IST)
    slots = [
        Slot(start=start + timedelta(hours=i), end=start + timedelta(hours=i + 1))
        for i in range(n_slots)
    ]
    summary = ProviderSummary(
        provider_id=pid,
        name=pid,
        service=Service.PLUMBER,
        area=Area.INDIRANAGAR,
        price_inr=500,
        rating=4.0,
        review_count=10,
        available_slots=slots,
        attributes=Attributes(verified=False, years_experience=2, languages=[], brands=[]),
    )
    return Eligible(summary, pid, slots)


def test_handles_follow_the_final_order_not_the_search_order() -> None:
    flagged = ReviewDigest(n_reviews=3, injection_flags=1, reader_model="m")
    # Search order was P1, P2; the final order puts P2 first.
    placed = [
        Placed(_eligible("P2", 2), 0.8, {"review_signal": 0.9}, None),
        Placed(_eligible("P1", 3), 0.6, {"review_signal": 0.0}, flagged),
    ]
    shortlist = assemble_shortlist(placed, [], brief_version=1, window_label="w")
    assert [c.provider_id for c in shortlist.candidates] == ["P2", "P1"]
    assert [[h.ref for h in c.slots] for c in shortlist.candidates] == [
        ["S1", "S2"],
        ["S3", "S4", "S5"],
    ]
    assert shortlist.candidates[1].badges == ["unverified_reviews"]
    assert shortlist.candidates[0].score == 0.8


def test_the_cap_drops_the_lowest_ranked_not_the_last_searched() -> None:
    placed = [Placed(_eligible(f"P{i}", 1), 1 - i / 100, {}, None) for i in range(10)]
    shortlist = assemble_shortlist(placed, [], brief_version=1, window_label="w")
    assert [c.provider_id for c in shortlist.candidates] == [f"P{i}" for i in range(8)]
    assert [(e.provider_id, e.reason) for e in shortlist.excluded] == [
        ("P8", "over_cap"),
        ("P9", "over_cap"),
    ]
