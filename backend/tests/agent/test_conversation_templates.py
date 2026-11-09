"""The code-written conversation templates (M8): no fit, out of coverage, and the card
data behind "Why this order" and the "Excluded" row."""

from __future__ import annotations

from datetime import datetime, timedelta

from errand.agent.templates import EXCLUSION_WORDS, shortlist_card
from errand.agent.templates_conversation import (
    covered_labels,
    no_fit_body,
    no_fit_result,
    raised_budget,
)
from errand.clock import IST
from errand.domain.brief import (
    AreaFact,
    Brief,
    BudgetFact,
    Provenance,
    ServiceFact,
    TimeWindowExpr,
    WindowFact,
)
from errand.domain.errand import Errand, Phase
from errand.domain.slots import Candidate, Exclusion, Shortlist, SlotHandle
from errand.domain.timewin import resolve
from errand.world.contracts import Area, Service, Slot

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
PROV = Provenance(source="user_msg", message_id="m1", evidence="x")


def window(phrase: str, **kw: object) -> WindowFact:
    expr = TimeWindowExpr(phrase=phrase, **kw)  # type: ignore[arg-type]
    resolved = resolve(expr, NOW, IST)
    assert not hasattr(resolved, "reason")
    return WindowFact(value=resolved, expr=expr, prov=PROV, set_at_turn=1)


def errand(budget: int | str = 1500, phrase: str = "this week", **kw: object) -> Errand:
    brief = Brief(
        version=1,
        service=ServiceFact(value=Service.PLUMBER, prov=PROV, set_at_turn=1),
        area=AreaFact(value=Area.INDIRANAGAR, prov=PROV, set_at_turn=1),
        window=window(phrase, **kw),
        budget_max_inr=BudgetFact(value=budget, prov=PROV, set_at_turn=1),  # type: ignore[arg-type]
    )
    return Errand(errand_id="e1", kind="book", phase=Phase.COMPARING, brief=brief)  # type: ignore[arg-type]


def test_the_no_fit_template_names_window_budget_area_in_that_order() -> None:
    result = no_fit_result(errand(part_of_day="morning"), "plumber, Indiranagar")
    assert [kind for kind, _, _ in result.widen] == ["window", "budget", "area"]
    _, body = no_fit_body(result)
    assert body.text == (  # type: ignore[union-attr]
        "No provider has a free slot for plumber, Indiranagar. I can widen the dates or "
        "times, your budget or the area. Which would you like?"
    )


def test_the_no_fit_template_leaves_out_what_the_brief_does_not_constrain() -> None:
    # no budget limit: nothing to raise; a bare "this week" has no daily bounds: try next week
    result = no_fit_result(errand(budget="none"), "")
    assert [(k, label, reply) for k, label, reply in result.widen] == [
        ("window", "Try next week", "next week"),
        ("area", "Try another area", "try a different area"),
    ]
    # "next week" cannot be widened by us: only budget and area remain
    assert [k for k, _, _ in no_fit_result(errand(phrase="next week"), "").widen] == [
        "budget",
        "area",
    ]


def test_the_budget_quick_reply_is_a_round_raise_the_user_can_still_refuse() -> None:
    assert raised_budget(400) == 1_000
    assert raised_budget(1500) == 2_500
    assert raised_budget(2000) == 3_000


def test_the_covered_values_are_listed_as_users_say_them() -> None:
    assert covered_labels("area") == ["Indiranagar", "Koramangala", "Whitefield", "Jayanagar"]
    assert covered_labels("service") == ["Plumber", "Electrician", "AC servicing"]


def test_a_shortlist_card_carries_chips_score_parts_and_every_exclusion() -> None:
    slot = Slot(start=NOW + timedelta(days=1), end=NOW + timedelta(days=1, hours=1))
    candidate = Candidate(
        provider_id="P1",
        name="Sanjay Plumbing",
        rating=4.6,
        price_inr=600,
        slots=[SlotHandle(ref="S1", provider_id="P1", slot=slot, search_price_inr=600)],
        score=0.7542,
        score_parts={"rating": 0.95, "price_headroom": 0.6, "slot_fit": 0.3333},
    )
    excluded = [
        Exclusion(provider_id="P2", name="A", reason="over_budget"),
        Exclusion(provider_id="P3", name="B", reason="no_slot_in_window"),
        Exclusion(provider_id="P4", name="C", reason="over_cap"),
        Exclusion(provider_id="P5", name="D", reason="avoided"),
    ]
    e = errand().model_copy(
        update={
            "shortlist": Shortlist(
                brief_version=1, window_label="w", candidates=[candidate], excluded=excluded
            )
        }
    )
    card = shortlist_card(e, None, IST)
    assert card.excluded_count == 4 and len(card.excluded) == 4
    assert [x.reason_text for x in card.excluded] == [
        EXCLUSION_WORDS[r] for r in ("over_budget", "no_slot_in_window", "over_cap", "avoided")
    ]
    assert all(x.reason_text and x.reason_text != "Left out" for x in card.excluded)
    assert card.candidates[0].score_parts == candidate.score_parts
    assert card.candidates[0].score == 0.7542
    assert [c.field.value for c in card.brief] == ["service", "area", "window", "budget_max_inr"]
    window_chip = card.brief[2]
    assert window_chip.window_label == "Mon 9 Nov - Sun 15 Nov" and window_chip.source == "user_msg"
