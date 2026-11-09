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
    assert not out.applied
    # the conflict is the kept 15:00 bound, so the planner must ask about hours, not days
    assert "morning cannot fit not before 15:00 kept from earlier" in out.errors[0]
    assert "ask whether they want morning or not before 15:00" in out.errors[0]
    assert "days" not in out.errors[0]


def test_any_time_drops_the_bounds_kept_from_earlier() -> None:
    first = say(
        Brief(),
        "Thursday, not before 10 a.m.",
        TimeWindowExpr(phrase="Thursday", not_before=time(10)),
        "Thursday, not before 10 a.m.",
        1,
    )
    assert label(first) == "Thu 12 Nov, from 10:00"
    wide = say(
        first,
        "any time Thursday",
        TimeWindowExpr(phrase="Thursday", part_of_day="any"),
        "any time Thursday",
        2,
    )
    assert label(wide) == "Thu 12 Nov"
    # a bound set in the same patch still applies
    bounded = say(
        first,
        "any time Thursday, but not after 4 pm",
        TimeWindowExpr(phrase="Thursday", part_of_day="any", not_after=time(16)),
        "any time Thursday, but not after 4 pm",
        2,
    )
    assert label(bounded) == "Thu 12 Nov, until 16:00"


# ---- a time bound said before the day (BUG-N1-1, 9.9)


def bound_patch(text: str, quotes: list[str], earlier: list[str], phrase: str = "tomorrow"):  # type: ignore[no-untyped-def]
    expr = TimeWindowExpr(phrase=phrase, not_before=time(10))
    return apply_patch(
        Brief(),
        sets=[FieldValue(BriefField.WINDOW, expr, quotes[0], tuple(quotes[1:]))],
        clears=[],
        message_text=text,
        message_id="m2",
        turn=2,
        now=NOW,
        tz=IST,
        earlier_messages=earlier,
    )


def test_a_bound_may_cite_an_earlier_message_of_the_errand() -> None:
    out = bound_patch(
        "tomorrow",
        ["tomorrow", "not before 10 a.m."],
        ["Book the same electrician as last time, but not before 10 a.m."],
    )
    assert out.applied, out.errors
    assert out.brief.window is not None
    assert out.brief.window.value.label.endswith("from 10:00")


def test_a_bound_in_no_message_is_still_rejected() -> None:
    out = bound_patch(
        "tomorrow", ["tomorrow", "not before 10 a.m."], ["Book the same electrician as last time"]
    )
    assert not out.applied
    assert "not in the user's message" in out.errors[0]


def test_a_quoted_bound_must_still_say_the_time_it_sets() -> None:
    out = bound_patch(
        "tomorrow", ["tomorrow", "not before 9 a.m."], ["something, not before 9 a.m."]
    )
    assert not out.applied
    assert "does not give not_before 10:00" in out.errors[0]


def test_the_day_phrase_cannot_come_from_an_earlier_message() -> None:
    out = bound_patch(
        "yes please",
        ["tomorrow", "not before 10 a.m."],
        ["Book it tomorrow, not before 10 a.m."],
    )
    assert not out.applied


def test_only_a_window_bound_reads_earlier_messages() -> None:
    out = apply_patch(
        Brief(),
        sets=[FieldValue(BriefField.BUDGET, 1500, "under 1500")],
        clears=[],
        message_text="yes please",
        message_id="m2",
        turn=2,
        now=NOW,
        tz=IST,
        earlier_messages=["a plumber, under 1500"],
    )
    assert not out.applied


async def test_earlier_messages_are_this_errands_only() -> None:
    """A bound said in an earlier errand of the same chat is not evidence for this one:
    the errand id names the run that created it, and only user messages from there on count."""
    from types import SimpleNamespace

    from errand.agent.nodes.plan import PlannerLoop
    from errand.store.repos.messages import Message

    def msg(run: str, role: str, text: str) -> Message:
        return Message(
            f"{run}-{role}-{text[:3]}", "c_1", None, role, "text",  # type: ignore[arg-type]
            {"text": text}, run, 1, None, None, None, 0,
        )  # fmt: skip

    history = [
        msg("r_0001", "user", "Plumber, not before 10 a.m."),
        msg("r_0001", "assistant", "Booked."),
        msg("r_0002", "user", "Book the same electrician"),
        msg("r_0002", "assistant", "Which day?"),
        msg("r_0003", "user", "tomorrow"),
    ]

    class Session:
        async def __aenter__(self) -> SimpleNamespace:
            async def listing(_: str) -> list[Message]:
                return history

            return SimpleNamespace(messages=SimpleNamespace(list_for_conversation=listing))

        async def __aexit__(self, *_: object) -> None:
            return None

    loop = object.__new__(PlannerLoop)
    loop.deps = SimpleNamespace(  # type: ignore[assignment]
        db=SimpleNamespace(session=Session), conversation_id="c_1"
    )
    loop.errand = SimpleNamespace(errand_id="e_r_0002")  # type: ignore[assignment]
    loop.inbound = SimpleNamespace(message_id="r_0003-user-tom")  # type: ignore[assignment]
    assert await loop._earlier_user_messages() == ["Book the same electrician"]
