"""INVALIDATES (7.3): what a changed brief field discards, all six rows.

The function is pure, so each row is tested directly, the dormant ones (provider
preference and the booking target, which only M9 and M11 can patch) included. M11's
forget and correct call it too.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Literal

from errand.agent.brief_ops import (
    INVALIDATES,
    FieldValue,
    Invalidation,
    apply_patch,
    invalidations,
)
from errand.agent.timewin import resolve
from errand.clock import IST
from errand.domain.brief import (
    AreaFact,
    Brief,
    BriefField,
    BudgetFact,
    Provenance,
    ProviderPref,
    ProviderPrefFact,
    ServiceFact,
    TextFact,
    TimeWindowExpr,
    WindowFact,
)
from errand.domain.errand import Errand, Phase
from errand.domain.slots import HoldRef, Shortlist
from errand.world.contracts import Area, Service, Slot

F = BriefField
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
PROV = Provenance(source="user_msg", message_id="m1", evidence="x")
HELD = Slot(start=datetime(2026, 11, 10, 9, tzinfo=IST), end=datetime(2026, 11, 10, 10, tzinfo=IST))


def window_fact(phrase: str, **kw: object) -> WindowFact:
    expr = TimeWindowExpr(phrase=phrase, **kw)  # type: ignore[arg-type]
    resolved = resolve(expr, NOW, IST)
    assert not hasattr(resolved, "reason"), resolved
    return WindowFact(value=resolved, expr=expr, prov=PROV, set_at_turn=1)


def brief(**kw: object) -> Brief:
    base: dict[str, object] = {
        "version": 1,
        "service": ServiceFact(value=Service.PLUMBER, prov=PROV, set_at_turn=1),
        "issue": TextFact(value="kitchen tap", prov=PROV, set_at_turn=1),
        "area": AreaFact(value=Area.INDIRANAGAR, prov=PROV, set_at_turn=1),
        "window": window_fact("this week", part_of_day="morning"),
        "budget_max_inr": BudgetFact(value=1500, prov=PROV, set_at_turn=1),
    }
    return Brief.model_validate({**base, **kw})


def errand_with(b: Brief, *, card: bool = True, quote: int = 1350) -> Errand:
    hold = HoldRef(
        hold_record_id="h1",
        hold_id="H-1",
        provider_id="P1",
        slot=HELD,
        search_price_inr=1200,
        quoted_price_inr=quote,
        expires_at=NOW + timedelta(minutes=10),
    )
    return Errand(
        errand_id="e1",
        kind="book",  # type: ignore[arg-type]
        phase=Phase.AWAITING_APPROVAL if card else Phase.COMPARING,
        brief=b,
        shortlist=Shortlist(brief_version=b.version, window_label="w", candidates=[]),
        hold=hold,
        approval_id="ap_1" if card else None,
    )


def whats(hits: list[Invalidation]) -> list[str]:
    return [h.what for h in hits]


def test_the_table_has_the_six_rows() -> None:
    assert set(INVALIDATES) == {
        F.SERVICE,
        F.AREA,
        F.WINDOW,
        F.BUDGET,
        F.PROVIDER_PREF,
        F.TARGET_BOOKING,
    }


def test_service_discards_shortlist_hold_card_and_issue() -> None:
    old = brief()
    new = brief(
        service=ServiceFact(value=Service.ELECTRICIAN, prov=PROV, set_at_turn=2), issue=None
    )
    hits = invalidations(F.SERVICE, old, new, errand_with(old), IST)
    assert whats(hits) == ["shortlist", "hold", "approval", "issue"]
    assert {h.reason for h in hits} == {"service_changed"}
    assert [h.id for h in hits][:3] == ["shortlist_v1", "H-1", "ap_1"]


def test_changing_the_service_clears_the_issue_in_the_same_merge() -> None:
    old = brief()
    out = apply_patch(
        old,
        sets=[FieldValue(F.SERVICE, Service.ELECTRICIAN, "an electrician")],
        clears=[],
        message_text="Actually I need an electrician",
        message_id="m2",
        turn=2,
        now=NOW,
        tz=IST,
    )
    assert out.applied and out.brief.service is not None
    assert out.brief.service.value is Service.ELECTRICIAN and out.brief.issue is None
    assert out.brief.version == old.version + 1  # one merge, one version
    assert out.cleared_fields == [F.ISSUE]
    # but a patch that carries a new issue keeps it, and a first fill clears nothing
    keep = apply_patch(
        old,
        sets=[
            FieldValue(F.SERVICE, Service.ELECTRICIAN, "an electrician"),
            FieldValue(F.ISSUE, "flickering light", "flickering light"),
        ],
        clears=[],
        message_text="an electrician for a flickering light",
        message_id="m2",
        turn=2,
        now=NOW,
        tz=IST,
    )
    assert keep.brief.issue is not None and keep.brief.issue.value == "flickering light"
    first = apply_patch(
        Brief(issue=old.issue),
        sets=[FieldValue(F.SERVICE, Service.PLUMBER, "plumber")],
        clears=[],
        message_text="plumber",
        message_id="m1",
        turn=1,
        now=NOW,
        tz=IST,
    )
    assert first.brief.issue is not None


def test_area_discards_shortlist_hold_and_card() -> None:
    old = brief()
    new = brief(area=AreaFact(value=Area.WHITEFIELD, prov=PROV, set_at_turn=2))
    hits = invalidations(F.AREA, old, new, errand_with(old), IST)
    assert whats(hits) == ["shortlist", "hold", "approval"]
    assert {h.reason for h in hits} == {"area_changed"}


def test_window_always_stales_the_shortlist_but_keeps_a_card_whose_slot_still_fits() -> None:
    old = brief()
    inside = brief(window=window_fact("this week"))  # mornings -> any time: 09:00 still fits
    hits = invalidations(F.WINDOW, old, inside, errand_with(old), IST)
    assert whats(hits) == ["shortlist"]


def test_window_discards_hold_and_card_when_the_held_slot_falls_outside() -> None:
    old = brief()
    outside = brief(window=window_fact("thursday or friday"))
    hits = invalidations(F.WINDOW, old, outside, errand_with(old), IST)
    assert whats(hits) == ["shortlist", "hold", "approval"]
    # by time of day too: the held slot is 09:00
    late = brief(window=window_fact("this week", not_before=time(10)))
    assert whats(invalidations(F.WINDOW, old, late, errand_with(old), IST)) == [
        "shortlist",
        "hold",
        "approval",
    ]
    # a cleared window admits no slot
    cleared = brief(window=None)
    assert "approval" in whats(invalidations(F.WINDOW, old, cleared, errand_with(old), IST))


def test_a_window_change_with_no_card_or_hold_only_stales_the_shortlist() -> None:
    old = brief()
    outside = brief(window=window_fact("thursday or friday"))
    bare = errand_with(old, card=False).model_copy(update={"hold": None})
    assert whats(invalidations(F.WINDOW, old, outside, bare, IST)) == ["shortlist"]


def test_budget_is_ranking_only_unless_the_held_quote_now_exceeds_it() -> None:
    old = brief()
    cases: list[tuple[int | Literal["none"], list[str]]] = [
        (2000, ["ranking"]),  # raised
        (1350, ["ranking"]),  # equal to the quote: not exceeded
        (1000, ["ranking", "approval"]),  # lowered below the 1,350 quote
        ("none", ["ranking"]),
    ]
    for value, expected in cases:
        new = brief(budget_max_inr=BudgetFact(value=value, prov=PROV, set_at_turn=2))
        hits = invalidations(F.BUDGET, old, new, errand_with(old), IST)
        assert whats(hits) == expected, value
    over = brief(budget_max_inr=BudgetFact(value=1000, prov=PROV, set_at_turn=2))
    hits = invalidations(F.BUDGET, old, over, errand_with(old), IST)
    assert [h.reason for h in hits] == ["budget_max_inr_changed", "quote_over_budget"]
    # a budget change never discards the hold: the quote is still the quote
    assert "hold" not in whats(hits)


def test_provider_pref_row_is_dormant_but_correct() -> None:
    old = brief()
    avoid = brief(
        provider_pref=ProviderPrefFact(
            value=ProviderPref(avoid_ids=["P1"]), prov=PROV, set_at_turn=2
        )
    )
    hits = invalidations(F.PROVIDER_PREF, old, avoid, errand_with(old), IST)
    assert whats(hits) == ["ranking", "approval"]
    assert hits[1].reason == "provider_avoided"
    other = brief(
        provider_pref=ProviderPrefFact(
            value=ProviderPref(avoid_ids=["P7"]), prov=PROV, set_at_turn=2
        )
    )
    assert whats(invalidations(F.PROVIDER_PREF, old, other, errand_with(old), IST)) == ["ranking"]


def test_target_booking_row_is_dormant_but_discards_everything_downstream() -> None:
    old = brief()
    new = brief(target_booking_id=TextFact(value="B-1", prov=PROV, set_at_turn=2))
    hits = invalidations(F.TARGET_BOOKING, old, new, errand_with(old), IST)
    assert whats(hits) == ["shortlist", "hold", "approval"]
    assert {h.reason for h in hits} == {"target_booking_id_changed"}


def test_an_unchanged_value_and_the_free_text_fields_discard_nothing() -> None:
    old = brief()
    same = brief(budget_max_inr=BudgetFact(value=1500, prov=PROV, set_at_turn=3))
    assert invalidations(F.BUDGET, old, same, errand_with(old), IST) == []
    new = brief(issue=TextFact(value="leaking pipe", prov=PROV, set_at_turn=2))
    assert invalidations(F.ISSUE, old, new, errand_with(old), IST) == []
    assert invalidations(F.NEW_WINDOW, old, new, errand_with(old), IST) == []


def test_nothing_to_discard_means_no_hit() -> None:
    old = brief()
    new = brief(area=AreaFact(value=Area.WHITEFIELD, prov=PROV, set_at_turn=2))
    empty = Errand(errand_id="e1", kind="book", phase=Phase.GATHERING, brief=old)  # type: ignore[arg-type]
    assert invalidations(F.AREA, old, new, empty, IST) == []
