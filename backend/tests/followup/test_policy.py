"""The notify policy, one test per row of Gate 3 decision 3 and one per ordering."""

from __future__ import annotations

from datetime import datetime, timedelta

from errand.clock import IST
from errand.followup.policy import Action, Decision, Facts, IgnoreReason, Urgency, decide

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


def facts(**kw: object) -> Facts:
    base: dict[str, object] = {"slot_start": NOW + timedelta(days=4), "now": NOW}
    return Facts(**{**base, **kw})  # type: ignore[arg-type]


def test_row_1_a_slot_that_has_started_is_ignored() -> None:
    d = decide(facts(slot_start=NOW - timedelta(hours=1)))
    assert d == Decision(Action.IGNORE, ignore_reason=IgnoreReason.SLOT_STARTED)
    assert not d.notifies and not d.searches
    assert decide(facts(slot_start=NOW)).action is Action.IGNORE


def test_row_1_a_replacement_that_already_succeeded_is_ignored() -> None:
    d = decide(facts(replacement_succeeded=True))
    assert d == Decision(Action.IGNORE, ignore_reason=IgnoreReason.ALREADY_REPLACED)


def test_row_2_a_same_service_booking_in_the_window_notifies_without_a_search() -> None:
    d = decide(facts(same_service_in_window="B-31"))
    assert d.action is Action.NOTIFY_NO_SEARCH and d.existing_booking_id == "B-31"
    assert d.notifies and not d.searches


def test_row_3_a_cancel_card_means_nothing_to_cancel_and_no_offer() -> None:
    d = decide(facts(cancel_card=True))
    assert d.action is Action.NOTIFY_NOTHING_TO_CANCEL
    assert d.notifies and not d.searches


def test_row_4_a_reschedule_card_searches_the_window_it_was_moving_to() -> None:
    d = decide(facts(reschedule_card=True))
    assert d.action is Action.NOTIFY_SEARCH and d.use_reschedule_window


def test_row_5_a_slot_within_24_hours_is_urgent() -> None:
    d = decide(facts(slot_start=NOW + timedelta(hours=24)))
    assert d.action is Action.NOTIFY_SEARCH and d.urgency is Urgency.URGENT
    assert decide(facts(slot_start=NOW + timedelta(hours=3))).urgency is Urgency.URGENT


def test_row_6_anything_else_is_normal() -> None:
    d = decide(facts(slot_start=NOW + timedelta(hours=24, minutes=1)))
    assert d == Decision(Action.NOTIFY_SEARCH, Urgency.NORMAL)


def test_first_match_wins() -> None:
    soon = NOW + timedelta(hours=5)
    # started beats everything
    assert (
        decide(
            facts(
                slot_start=NOW - timedelta(minutes=1),
                same_service_in_window="B-1",
                cancel_card=True,
                reschedule_card=True,
            )
        ).action
        is Action.IGNORE
    )
    # replaced beats the notices
    assert decide(facts(replacement_succeeded=True, cancel_card=True)).action is Action.IGNORE
    # a same-service booking beats the cards and urgency
    d = decide(facts(slot_start=soon, same_service_in_window="B-1", cancel_card=True))
    assert d.action is Action.NOTIFY_NO_SEARCH and d.urgency is Urgency.NORMAL
    # a cancel card beats a reschedule card and urgency
    d = decide(facts(slot_start=soon, cancel_card=True, reschedule_card=True))
    assert d.action is Action.NOTIFY_NOTHING_TO_CANCEL and d.urgency is Urgency.NORMAL
    # a reschedule card beats plain urgency but keeps it
    d = decide(facts(slot_start=soon, reschedule_card=True))
    assert d.use_reschedule_window and d.urgency is Urgency.URGENT
