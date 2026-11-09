"""A window set over an existing one keeps what the new expression leaves unset (7.3).

"Actually Thursday or Friday" after "mornings only" must not drop the mornings; "any
time" is the explicit way to widen, and it needs its own evidence.
"""

from __future__ import annotations

from datetime import datetime, time

from errand.agent.brief_ops import FieldValue, apply_patch, carry_over
from errand.clock import IST
from errand.domain.brief import Brief, BriefField, TimeWindowExpr

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


def say(brief: Brief, text: str, expr: TimeWindowExpr, quote: str, turn: int) -> Brief:
    out = apply_patch(
        brief,
        sets=[FieldValue(BriefField.WINDOW, expr, quote)],
        clears=[],
        message_text=text,
        message_id=f"m{turn}",
        turn=turn,
        now=NOW,
        tz=IST,
    )
    assert out.applied, out.errors
    return out.brief


def label(brief: Brief) -> str:
    assert brief.window is not None
    return brief.window.value.label


def test_unset_sub_fields_come_from_the_old_expression() -> None:
    old = TimeWindowExpr(phrase="this week", part_of_day="morning", not_before=time(8))
    merged = carry_over(TimeWindowExpr(phrase="thursday or friday"), old)
    assert merged == TimeWindowExpr(
        phrase="thursday or friday", part_of_day="morning", not_before=time(8)
    )
    # a sub-field the patch sets wins
    merged = carry_over(TimeWindowExpr(phrase="friday", part_of_day="evening"), old)
    assert (merged.part_of_day, merged.not_before) == ("evening", time(8))
    assert carry_over(TimeWindowExpr(phrase="friday"), None) == TimeWindowExpr(phrase="friday")


def test_a_days_only_correction_keeps_the_mornings() -> None:
    first = say(
        Brief(),
        "this week, mornings only",
        TimeWindowExpr(phrase="this week", part_of_day="morning"),
        "this week, mornings only",
        1,
    )
    second = say(
        first,
        "Actually Thursday or Friday",
        TimeWindowExpr(phrase="Thursday or Friday"),
        "Thursday or Friday",
        2,
    )
    assert label(second) == "Thu 12 Nov - Fri 13 Nov, 07:00-12:00"
    assert second.window is not None and second.window.expr is not None
    assert second.window.expr.part_of_day == "morning"  # stored, so a third correction carries too


def test_not_before_joins_the_carried_day_part() -> None:
    first = say(
        Brief(),
        "this week, mornings only",
        TimeWindowExpr(phrase="this week", part_of_day="morning"),
        "this week, mornings only",
        1,
    )
    second = say(
        first,
        "Thursday or Friday, not before 10 a.m.",
        TimeWindowExpr(phrase="Thursday or Friday", not_before=time(10)),
        "Thursday or Friday, not before 10 a.m.",
        2,
    )
    assert label(second) == "Thu 12 Nov - Fri 13 Nov, 10:00-12:00"


def test_any_time_widens_and_needs_its_own_evidence() -> None:
    first = say(
        Brief(),
        "this week, mornings only",
        TimeWindowExpr(phrase="this week", part_of_day="morning"),
        "this week, mornings only",
        1,
    )
    wide = say(
        first,
        "Friday, any time",
        TimeWindowExpr(phrase="Friday", part_of_day="any"),
        "Friday, any time",
        2,
    )
    assert label(wide) == "Fri 13 Nov"
    refused = apply_patch(
        first,
        sets=[
            FieldValue(
                BriefField.WINDOW, TimeWindowExpr(phrase="Friday", part_of_day="any"), "Friday"
            )
        ],
        clears=[],
        message_text="Friday",
        message_id="m3",
        turn=3,
        now=NOW,
        tz=IST,
    )
    assert not refused.applied and "any" in refused.errors[0]


def test_a_carried_bound_that_empties_the_window_is_refused_not_guessed() -> None:
    first = say(
        Brief(),
        "Friday, after 3 pm",
        TimeWindowExpr(phrase="Friday", not_before=time(15)),
        "Friday, after 3 pm",
        1,
    )
    out = apply_patch(
        first,
        sets=[
            FieldValue(
                BriefField.WINDOW,
                TimeWindowExpr(phrase="Friday", part_of_day="morning"),
                "Friday morning",
            )
        ],
        clears=[],
        message_text="Friday morning",
        message_id="m2",
        turn=2,
        now=NOW,
        tz=IST,
    )
    assert not out.applied and "empty" in out.errors[0]
