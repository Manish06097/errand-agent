"""check_evidence: the span rule of M3 plus the value-bound rules of 10.7 (M8)."""

from __future__ import annotations

from datetime import time
from typing import Any

from errand.agent.brief_ops import check_evidence
from errand.domain.brief import BriefField, TimeWindowExpr
from errand.world.contracts import Area, Service

F = BriefField
MESSAGE = (
    "Find a plumber in Indira Nagar for my kitchen tap on Thursday or Friday, "
    "not before 10 a.m., under ₹1,500."
)


def ok(quote: str, name: BriefField, value: Any, *more: str, message: str = MESSAGE) -> bool:
    return check_evidence(quote, message, name, value, more).ok


def window(phrase: str = "Thursday or Friday", **kw: Any) -> TimeWindowExpr:
    return TimeWindowExpr(phrase=phrase, **kw)


def test_the_span_rule_still_holds() -> None:
    assert not ok("abc", F.BUDGET, 1500)  # under 4 characters
    assert not ok("under ₹2,000", F.BUDGET, 2000)  # not in the message
    assert ok("UNDER   ₹1,500", F.BUDGET, 1500)  # case and whitespace normalised
    assert ok("kitchen tap", F.BUDGET, None)  # a clear needs the span only


def test_a_real_quote_that_does_not_contain_the_value_is_rejected() -> None:
    # S60: "under" is a substring of the message, and says nothing about 5,000
    assert not ok("Find a plumber in Indira Nagar for my kitchen tap", F.BUDGET, 5000)
    assert not ok("under", F.BUDGET, 5000)
    assert not ok("under ₹1,500", F.BUDGET, 5000)
    assert ok("under ₹1,500", F.BUDGET, 1500)


def test_none_needs_a_no_limit_phrase() -> None:
    msg = "any price is fine, it doesn't matter"
    assert check_evidence("any price is fine", msg, F.BUDGET, "none").ok
    assert not check_evidence("under ₹1,500", MESSAGE, F.BUDGET, "none").ok


def test_budget_forms() -> None:
    for span, value in [
        ("₹1,500", 1500),
        ("rs 1500", 1500),
        ("1.5k", 1500),
        ("fifteen hundred", 1500),
    ]:
        assert check_evidence(span, f"under {span} please", F.BUDGET, value).ok, span


def test_window_phrase_must_be_in_the_span() -> None:
    assert ok("Thursday or Friday, not before 10 a.m.", F.WINDOW, window(not_before=time(10)))
    assert not ok("kitchen tap", F.WINDOW, window())
    assert not ok("Thursday or Friday", F.WINDOW, window("next week"))


def test_times_must_parse_to_the_same_time() -> None:
    quote = "Thursday or Friday, not before 10 a.m."
    assert ok(quote, F.WINDOW, window(not_before=time(10)))
    assert not ok(quote, F.WINDOW, window(not_before=time(11)))
    assert not ok(quote, F.WINDOW, window(not_before=time(22)))  # a.m. is one reading
    assert not ok(quote, F.WINDOW, window(not_after=time(10, 30)))


def test_a_bare_hour_has_two_readings_and_either_is_fine() -> None:
    msg = "Thursday, after 5"
    quote = "Thursday, after 5"
    assert check_evidence(quote, msg, F.WINDOW, window("Thursday", not_before=time(17))).ok
    assert check_evidence(quote, msg, F.WINDOW, window("Thursday", not_before=time(5))).ok
    assert not check_evidence(quote, msg, F.WINDOW, window("Thursday", not_before=time(18))).ok


def test_not_before_ten_supports_ten() -> None:
    msg = "Thursday, not before ten"
    assert check_evidence(
        "not before ten", msg, F.WINDOW, window("Thursday", not_before=time(10)), ("Thursday",)
    ).ok


def test_two_spans_for_one_field_are_read_together() -> None:
    assert ok("Thursday or Friday", F.WINDOW, window(not_before=time(10)), "not before 10 a.m.")
    # ... but every span must itself be in the message
    assert not ok("Thursday or Friday", F.WINDOW, window(not_before=time(10)), "not before 9")


def test_area_and_service_need_one_of_their_aliases() -> None:
    assert ok("Indira Nagar", F.AREA, Area.INDIRANAGAR)
    assert not ok("Indira Nagar", F.AREA, Area.KORAMANGALA)
    assert ok("plumber in Indira", F.SERVICE, Service.PLUMBER)
    assert not ok("plumber in Indira", F.SERVICE, Service.ELECTRICIAN)
    assert not ok("kitchen tap on Thursday", F.AREA, Area.INDIRANAGAR)


def test_issue_words_must_all_be_in_the_span() -> None:
    assert ok("my kitchen tap", F.ISSUE, "kitchen tap")
    assert not ok("my kitchen tap", F.ISSUE, "bathroom geyser")
    assert not ok("my kitchen tap", F.ISSUE, "kitchen sink leaking")


def test_a_day_part_the_patch_sets_needs_a_day_part_word_in_the_spans() -> None:
    # M8 review: part_of_day is tied to evidence like every other value (10.7)
    msg = "Find a plumber this week, mornings only, under ₹1,500."
    quote = "this week, mornings only"

    def part(value: str, span: str = quote) -> bool:
        return check_evidence(span, msg, F.WINDOW, window("this week", part_of_day=value)).ok

    assert part("morning")
    assert not part("evening")
    assert not part("afternoon")
    assert not part("any")
    assert not part("evening", "this week")  # the phrase alone says nothing about the day part
    assert not part("morning", "this week")


def test_day_part_words_and_any_time() -> None:
    for text, value in [
        ("friday afternoon", "afternoon"),
        ("friday afternoons", "afternoon"),
        ("friday evening", "evening"),
        ("friday, any time", "any"),
        ("friday anytime", "any"),
    ]:
        assert check_evidence(text, text, F.WINDOW, window("friday", part_of_day=value)).ok, text
    assert not check_evidence(
        "friday evening", "friday evening", F.WINDOW, window("friday", part_of_day="any")
    ).ok


def test_a_window_without_a_day_part_needs_no_day_part_word() -> None:
    # the part carried over from the old window is not in the patch, so it is not checked
    assert ok("Thursday or Friday", F.WINDOW, window())
