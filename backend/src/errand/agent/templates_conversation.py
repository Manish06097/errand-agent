"""Code-written conversation templates (M8): out of coverage and no fit.

Model text never reaches these. The out-of-coverage question lists what we do cover as
quick replies; the no-fit message names what could widen the search, in the order
window, budget, area, and only for what the brief actually constrains (7.3, 10.2).
"""

from __future__ import annotations

import math

from errand.agent.cards import brief_chips, service_words
from errand.domain.brief import BriefField
from errand.domain.errand import Errand
from errand.domain.messages import ClarifyCard, MessageBody, NoticeAction, OutcomeNotice
from errand.domain.outcomes import NoFit, OutOfCoverage, RunOutcome
from errand.world.contracts import Area, Service

BUDGET_STEP = 500
BUDGET_RAISE = 1.5


def covered_labels(field: str) -> list[str]:
    """The covered values as the user would say them, in catalog order."""
    if field == "area":
        return [a.value.title() for a in Area]
    return [service_words(s.value) for s in Service]


def out_of_coverage_body(result: OutOfCoverage, errand: Errand) -> tuple[RunOutcome, MessageBody]:
    noun, plural = ("area", "areas") if result.field == "area" else ("service", "services")
    listed = ", ".join(result.covered)
    if result.asked:
        question = (
            f"Sorry, I don't cover {result.words} yet. The {plural} I cover are {listed}. "
            f"Which {noun} should I use?"
        )
        return RunOutcome.ASKED_USER, ClarifyCard(
            errand_id=errand.errand_id,
            fields=[BriefField(result.field)],
            question=question,
            options=result.covered,
            brief=brief_chips(errand.brief),
        )
    text = (
        f"Sorry, I don't cover {result.words} yet. The {plural} I cover are {listed}. "
        f"Your request stays as it was; tell me if you want a different {noun}."
    )
    return RunOutcome.ASKED_USER, OutcomeNotice(
        outcome=RunOutcome.ASKED_USER,
        text=text,
        actions=[NoticeAction(kind="answer", label="Answer")],
    )


def raised_budget(budget: int) -> int:
    return math.ceil(budget * BUDGET_RAISE / BUDGET_STEP) * BUDGET_STEP


def no_fit_result(errand: Errand, summary: str) -> NoFit:
    """What could widen, from the brief's own constraints."""
    brief = errand.brief
    widen: list[tuple[str, str, str]] = []
    window = brief.window
    if window is not None:
        phrase = window.expr.phrase if window.expr is not None else None
        if phrase and (window.value.daily_from or window.value.daily_to):
            widen.append(("window", "Any time of day", f"{phrase}, any time of day"))
        elif phrase and phrase.strip().casefold() != "next week":
            widen.append(("window", "Try next week", "next week"))
    budget = brief.budget_max_inr
    if budget is not None and isinstance(budget.value, int):
        higher = raised_budget(budget.value)
        widen.append(("budget", f"Raise budget to ₹{higher:,}", f"up to ₹{higher:,}"))
    if brief.area is not None:
        widen.append(("area", "Try another area", "try a different area"))
    return NoFit(widen=widen, summary=summary)


_WIDEN_WORDS = {"window": "the dates or times", "budget": "your budget", "area": "the area"}


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


def no_fit_body(result: NoFit) -> tuple[RunOutcome, MessageBody]:
    what = f" for {result.summary}" if result.summary else ""
    names = [_WIDEN_WORDS[kind] for kind, _, _ in result.widen]
    tail = f" I can widen {_join(names)}. Which would you like?" if names else ""
    text = f"No provider has a free slot{what}.{tail}"
    return RunOutcome.REPLIED, OutcomeNotice(
        outcome=RunOutcome.REPLIED,
        text=text,
        actions=[
            NoticeAction(kind="quick_reply", label=lb, reply=rp) for _, lb, rp in result.widen
        ],
    )
