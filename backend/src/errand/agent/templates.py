"""Outcome templates: every run ends with one code-written message (15.2).

Model text is used in exactly two places: the whole message for REPLIED, and the
one-line recommendation on a shortlist card. Everything else is a template over
structured facts, so a limit, an outage or a crash still reads clearly and never
claims anything happened that did not.

The one exception is the ClarifyCard: `ask_user(fields, question, options)` is the
model's question by contract. It is bounded and cleaned in the plan node before it
gets here (question 400 chars; at most 6 options of 40 chars; `clean_text` on each),
and the fields it asks for are code-checked by the ask guard.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.domain.brief import BriefField
from errand.domain.errand import Errand
from errand.domain.messages import (
    CandidateOption,
    ClarifyCard,
    MessageBody,
    NoticeAction,
    OutcomeNotice,
    ShortlistCard,
    SlotOption,
    TextBody,
)
from errand.domain.outcomes import (
    Ask,
    AskExhausted,
    Failure,
    Limit,
    Options,
    PendingCheck,
    PlanResult,
    Proposal,
    Reply,
    RunOutcome,
    TooLarge,
    ToolDown,
)
from errand.domain.windows import slot_label

RECOMMENDATION_MAX_CHARS = 240
_FIELD_WORDS: dict[BriefField, str] = {
    BriefField.SERVICE: "which service you need",
    BriefField.ISSUE: "what the job is",
    BriefField.AREA: "your area",
    BriefField.WINDOW: "when you want the visit",
    BriefField.BUDGET: "your budget",
    BriefField.PROVIDER_PREF: "your provider preference",
    BriefField.TARGET_BOOKING: "which booking you mean",
    BriefField.NEW_WINDOW: "the new time",
}
_TOOL_WORDS = {
    "search_providers": "Provider search",
    "get_provider": "Provider details",
    "list_bookings": "Your bookings list",
}


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _so_far(errand: Errand | None) -> str:
    """The brief's filled search fields in plain words, for a notice that stops early."""
    if errand is None:
        return ""
    brief = errand.brief
    parts: list[str] = []
    if brief.service is not None:
        parts.append(brief.service.value.value.replace("_", " "))
    if brief.area is not None:
        parts.append(brief.area.value.value.replace("_", " ").title())
    if brief.window is not None:
        parts.append(brief.window.value.label)
    if brief.budget_max_inr is not None:
        budget = brief.budget_max_inr.value
        parts.append("any price" if budget == "none" else f"under ₹{budget:,}")
    return ", ".join(parts)


def internal_error(run_id: str) -> OutcomeNotice:
    return OutcomeNotice(
        outcome=RunOutcome.INTERNAL_ERROR,
        text=(
            "Something went wrong on my side, so I stopped. Nothing was booked or changed. "
            f"(run {run_id})"
        ),
        actions=[NoticeAction(kind="retry", label="Try again")],
    )


def recommendation_line(text: str) -> str | None:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not line:
        return None
    if len(line) > RECOMMENDATION_MAX_CHARS:
        line = line[: RECOMMENDATION_MAX_CHARS - 3].rstrip() + "..."
    return line


def hidden_reviews_notice(flags: int) -> str | None:
    """The card's code-written line for a provider with flagged reviews."""
    if flags <= 0:
        return None
    if flags == 1:
        return "1 review hidden: it contained instructions to the assistant"
    return f"{flags} reviews hidden: they contained instructions to the assistant"


def shortlist_card(errand: Errand, recommendation: str | None, tz: tzinfo) -> ShortlistCard:
    shortlist = errand.shortlist
    assert shortlist is not None
    return ShortlistCard(
        errand_id=errand.errand_id,
        brief_version=shortlist.brief_version,
        window_label=shortlist.window_label,
        candidates=[
            CandidateOption(
                provider_id=c.provider_id,
                name=c.name,
                rating=c.rating,
                price_inr=c.price_inr,
                verified=c.verified,
                slots=[
                    SlotOption(
                        ref=h.ref,
                        start=h.slot.start.isoformat(),
                        end=h.slot.end.isoformat(),
                        label=slot_label(h.slot, tz),
                    )
                    for h in c.slots
                ],
                badges=list(c.badges),
                injection_flags=c.digest.injection_flags if c.digest else 0,
                notice=hidden_reviews_notice(c.digest.injection_flags if c.digest else 0),
            )
            for c in shortlist.candidates
        ],
        excluded_count=len(shortlist.excluded),
        recommendation=recommendation,
    )


def render(
    result: PlanResult | None, errand: Errand | None, run_id: str, tz: tzinfo
) -> tuple[RunOutcome, MessageBody]:
    if isinstance(result, Reply):
        text = result.text.strip() or "I don't have anything to add yet. What would you like to do?"
        return RunOutcome.REPLIED, TextBody(text=text)
    if isinstance(result, Ask) and errand is not None:
        return RunOutcome.ASKED_USER, ClarifyCard(
            errand_id=errand.errand_id,
            fields=result.fields,
            question=result.question,
            options=result.options,
        )
    if isinstance(result, AskExhausted):
        needed = _join([_FIELD_WORDS[f] for f in result.still_needed]) or "a few more details"
        return RunOutcome.ASKED_USER, OutcomeNotice(
            outcome=RunOutcome.ASKED_USER,
            text=(
                f"I still need {needed} before I can look for providers. "
                "Tell me whenever you're ready."
            ),
            actions=[NoticeAction(kind="answer", label="Answer")],
            still_needed=result.still_needed,
        )
    if isinstance(result, Options) and errand is not None and errand.shortlist is not None:
        return RunOutcome.PRESENTED_OPTIONS, shortlist_card(errand, result.recommendation, tz)
    if isinstance(result, Limit):
        known = _so_far(errand)
        so_far = f" So far I have: {known}." if known else ""
        return RunOutcome.LIMIT_REACHED, OutcomeNotice(
            outcome=RunOutcome.LIMIT_REACHED,
            text=f"I hit my step limit for this turn, so I stopped.{so_far} Nothing was booked.",
            actions=[
                NoticeAction(kind="retry", label="Try again"),
                NoticeAction(kind="narrow_search", label="Narrow search"),
            ],
        )
    if isinstance(result, ToolDown):
        what = _TOOL_WORDS.get(result.tool, "A provider service")
        return RunOutcome.TOOL_UNAVAILABLE, OutcomeNotice(
            outcome=RunOutcome.TOOL_UNAVAILABLE,
            text=f"{what} is down right now. Nothing was booked; try again in a moment.",
            actions=[NoticeAction(kind="retry", label="Retry")],
        )
    if isinstance(result, TooLarge):
        return RunOutcome.CONTEXT_TOO_LARGE, OutcomeNotice(
            outcome=RunOutcome.CONTEXT_TOO_LARGE,
            text="That message is too long for me; can you shorten it?",
            actions=[NoticeAction(kind="shorten", label="Shorten it")],
        )
    if isinstance(result, Failure | Proposal) or result is None:
        return RunOutcome.INTERNAL_ERROR, internal_error(run_id)
    return RunOutcome.INTERNAL_ERROR, internal_error(run_id)


# ---------------------------------------------------------------- booking failure paths (M5)
#
# Every sentence below is code over records. None of them uses a word the claim guard or
# INV-04 reads as a success claim without a record behind it: "booked twice", "cancelled"
# and "moved" all trip them, so these say "two bookings", "pulled out" and "changed the
# slot". A test runs each one through both.


def clock_hhmm(iso: str, tz: tzinfo) -> str:
    """'09:12' in the user's zone, from an ISO time with an offset."""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%H:%M")


def pending_text(result: PendingCheck, tz: tzinfo) -> str:
    """PENDING_VERIFICATION: the provider has not settled what `book` did (9.4 step 6)."""
    if result.reason == "fenced_off":
        return (
            "Another request took over before this one was sent, so nothing was sent "
            "from this request. Please check your bookings, or ask me again."
        )
    if result.reason == "material_mismatch":
        which = f" ({result.booking_id})" if result.booking_id else ""
        return (
            f"The provider recorded {result.detail}{which}, so I'm not treating this one as "
            "final. I'll sort it out with you here."
        )
    if result.reason == "check_incomplete":
        return (
            "I couldn't finish checking the booking with the provider, so I'm not treating "
            "this one as final. I'll keep checking and update you here."
        )
    safe = "The provider hasn't confirmed yet, and it can't end up as two bookings."
    if result.reason == "hold_live" and result.lapse_at:
        when = clock_hhmm(result.lapse_at, tz)
        return (
            f"{safe} I'll know for sure by {when}, when the hold lapses, and I'll update you here."
        )
    # The status could not be read: no time is promised, because none can be kept.
    return f"{safe} I'm still checking and will update you here."


def requote_text(reason: str | None, detail: str | None) -> str | None:
    """The line above a new card that replaced one the user had already approved."""
    if reason == "hold_lapsed":
        return (
            "The hold ran out before I could book, so nothing was booked. "
            "I held the slot again; please check the new card."
        )
    if reason == "price_changed":
        return (
            f"The hold ran out and the price changed ({detail}), so nothing was booked. "
            "Please check the new card."
        )
    if reason == "slot_changed":
        return (
            f"The hold ran out and the provider changed the slot ({detail}), so nothing was "
            "booked. Please check the new card."
        )
    if reason == "material_changed":
        return (
            "Something changed since you approved, so nothing was booked. "
            "Please check the updated card."
        )
    return None


def provider_cancelled_text(provider_name: str, slot_label_: str) -> str:
    """Decision 12: the booking was taken, then the provider pulled out. Both facts."""
    return (
        f"{provider_name} took the request for {slot_label_} and then pulled out on their "
        "side. You have no visit with them now."
    )


SLOT_TAKEN_TEXT = (
    "The provider says that slot was just taken by someone else, so nothing was booked."
)
NOT_BOOKED_TEXT = (
    "The booking did not go through, and nothing was booked. The hold has lapsed, so I "
    "need a fresh quote."
)
