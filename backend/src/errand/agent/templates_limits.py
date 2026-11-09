"""Templates for the limit and outage outcomes N3 adds (15.2).

Kept out of `templates.py`, which every milestone edits. Everything here is a template
over code facts: what the brief holds, the errand's phase, which tool failed. Model text
never appears in it, and every one of these says nothing was booked.
"""

from __future__ import annotations

from collections.abc import Mapping

from errand.agent import brief_ops
from errand.domain.brief import BriefField
from errand.domain.errand import Errand, Phase
from errand.domain.messages import NoticeAction, OutcomeNotice
from errand.domain.outcomes import RunOutcome


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def still_to_do(errand: Errand | None, field_words: Mapping[BriefField, str]) -> str:
    """What is left of the errand, from its phase and the brief's missing required fields."""
    if errand is None:
        return ""
    gaps = brief_ops.missing(errand.brief, errand.kind)
    if gaps:
        return "ask about " + _join([field_words[f] for f in gaps])
    if errand.phase is Phase.GATHERING or errand.shortlist is None:
        return "search providers"
    if errand.phase is Phase.COMPARING:
        return "compare providers"
    if errand.phase is Phase.AWAITING_APPROVAL:
        return "wait for your approval"
    return ""


def wall_clock_exceeded(known: str, pending: str) -> OutcomeNotice:
    so_far = f" So far I have: {known}." if known else ""
    todo = f" Still to do: {pending}." if pending else ""
    return OutcomeNotice(
        outcome=RunOutcome.WALL_CLOCK_EXCEEDED,
        text=f"I ran out of time for this turn, so I stopped.{so_far}{todo} Nothing was booked.",
        actions=[
            NoticeAction(kind="retry", label="Try again"),
            NoticeAction(kind="narrow_search", label="Narrow search"),
        ],
    )


def model_unavailable() -> OutcomeNotice:
    return OutcomeNotice(
        outcome=RunOutcome.MODEL_UNAVAILABLE,
        text="I can't reach my planning model right now, so I stopped. Nothing was booked.",
        actions=[NoticeAction(kind="retry", label="Try again")],
    )


def tool_unexpected() -> OutcomeNotice:
    return OutcomeNotice(
        outcome=RunOutcome.TOOL_UNAVAILABLE,
        text=(
            "A provider service sent back something I couldn't read, so I stopped. "
            "Nothing was booked."
        ),
        actions=[NoticeAction(kind="retry", label="Retry")],
    )


def model_refused() -> OutcomeNotice:
    return OutcomeNotice(
        outcome=RunOutcome.MODEL_REFUSED,
        text="I can't help with that request. Nothing was booked.",
        actions=[],
    )


def model_truncated() -> OutcomeNotice:
    return OutcomeNotice(
        outcome=RunOutcome.MODEL_NO_PROGRESS,
        text=(
            "My answer kept getting cut off, so I stopped. Nothing was booked. "
            "Try asking again, or in smaller steps."
        ),
        actions=[NoticeAction(kind="retry", label="Try again")],
    )
