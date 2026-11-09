"""Message bodies as stored in `messages.body_json` and served by the API.

This is the single definition of what the user sees; the web types are generated from
it in M7. `kind` on the messages row follows the body: text, card or notice.

`status_line` is the code-rendered line `respond` puts under the last message of a run
whenever the errand is not completed ("Not booked yet: waiting for your approval"), so
a paraphrased claim in model text is never the only signal on screen (10.8).

Approval cards (M4): `ActionCard` is rendered from, and stored as, `approvals.card_json`;
the UI shows it only from that row. Its `material` is the exact object the fingerprint
was computed from. `CardReminder` repeats a live card as the last message after a run
said something below it; `typed_grant_ok` says whether a typed "book it" may bind to it
(8.3). `OutcomeCard` is the verified result. The approval's current status is not part
of any card body: the API adds it from the approvals row when it serves the transcript.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.brief import BriefField
from errand.domain.material import Material
from errand.domain.outcomes import RunOutcome
from errand.domain.slots import Badge


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBody(_Body):
    type: Literal["text"] = "text"
    text: str
    status_line: str | None = None


class ClarifyCard(_Body):
    """A question for the fields the brief still lacks, with quick replies."""

    type: Literal["clarify"] = "clarify"
    errand_id: str
    fields: list[BriefField]
    question: str
    options: list[str] = Field(default_factory=list)
    status_line: str | None = None


class SlotOption(_Body):
    ref: str
    start: str  # ISO 8601 with +05:30
    end: str
    label: str  # "Tue 10 Nov, 09:00-10:00"


class CandidateOption(_Body):
    provider_id: str
    name: str
    rating: float
    price_inr: int
    verified: bool
    slots: list[SlotOption]
    # Server data for the injection badge (M10): code decides all three from the
    # quarantined reader's flags, never from review text or model output.
    badges: list[Badge] = Field(default_factory=list)
    injection_flags: int = 0
    # "1 review hidden: it contained instructions to the assistant"
    notice: str | None = None


class ShortlistCard(_Body):
    """ProviderComparisonCard data: candidates in code order, slot buttons by handle."""

    type: Literal["shortlist"] = "shortlist"
    errand_id: str
    brief_version: int
    window_label: str
    candidates: list[CandidateOption]
    excluded_count: int = 0
    recommendation: str | None = None
    status_line: str | None = None


class NoticeAction(_Body):
    kind: Literal[
        "retry", "narrow_search", "shorten", "answer", "refresh_hold", "raise_budget", "options"
    ]
    label: str
    # For refresh_hold: the approval whose slot gets a fresh hold and a new card.
    approval_id: str | None = None


class PendingFacts(_Body):
    """The facts behind a PENDING_VERIFICATION notice (M5), for the card M7 builds on it.

    The text is the whole message; these fields let a card show the same facts in its own
    layout without parsing it. No "Check now" or "Never mind" button exists yet: M6 adds
    the actions and their routes, and the notice carries facts only until then.
    """

    reason: Literal[
        "hold_live", "status_unavailable", "material_mismatch", "check_incomplete", "fenced_off"
    ]
    intent_id: str
    # hold_live (the wait rule): when the hold lapses by the world clock, ISO with +05:30,
    # and the same time as the user reads it ("09:12", IST).
    lapse_at: str | None = None
    lapse_label: str | None = None
    # material_mismatch: the provider's booking, and what it recorded that was not approved.
    booking_id: str | None = None
    recorded: str | None = None


class OutcomeNotice(_Body):
    """A code-written outcome message: limits, outages, errors, a spent ask budget."""

    type: Literal["notice"] = "notice"
    outcome: RunOutcome
    text: str
    actions: list[NoticeAction] = Field(default_factory=list)
    still_needed: list[BriefField] = Field(default_factory=list)
    # Set on PENDING_VERIFICATION notices (M5).
    pending: PendingFacts | None = None
    status_line: str | None = None


class MaterialLine(_Body):
    label: str  # "Provider", "When", "Price"
    value: str


class NearbyBooking(_Body):
    """A confirmed booking of the same service within a day of the card's slot."""

    booking_id: str
    provider_name: str
    label: str  # "Sat 14 Nov, 10:00-11:00"


ApprovalAction = Literal["book", "cancel", "reschedule"]


class ActionCard(_Body):
    type: Literal["action"] = "action"
    approval_id: str
    errand_id: str
    action: ApprovalAction
    fingerprint: str
    title: str  # "Book Ravi Plumbing"
    lines: list[MaterialLine]
    provider_id: str
    provider_name: str
    service: str
    slot_start: str  # ISO 8601 with +05:30
    slot_end: str
    slot_label: str
    search_price_inr: int
    quoted_price_inr: int  # the held quote: this is the price approved
    price_delta_inr: int  # quoted minus search price
    # The hold the card was issued on; approvals.card_json is never rewritten, so this is the
    # record the one automatic re-hold is measured against. Absent on pre-M5 cards.
    hold_id: str | None = None
    hold_expires_at: str  # world clock
    expires_at: str  # the approval's own expiry, agent clock
    button_label: str  # "Book for ₹1,350"
    decline_label: str = "Decline"
    nearby: list[NearbyBooking] = Field(default_factory=list)
    material: Material
    status_line: str | None = None


class CardReminder(_Body):
    type: Literal["reminder"] = "reminder"
    approval_id: str
    fingerprint: str
    text: str  # "Still waiting on: Book Ravi Plumbing, Thu 13 Nov 09:00, ₹1,350"
    button_label: str
    decline_label: str = "Decline"
    typed_grant_ok: bool
    hint: str  # "Tap the button to confirm" when typed_grant_ok is false
    status_line: str | None = None


class OutcomeCard(_Body):
    """A verified result. `verified` is true only for *_VERIFIED outcomes."""

    type: Literal["outcome"] = "outcome"
    outcome: RunOutcome
    verified: bool
    title: str
    text: str
    lines: list[MaterialLine] = Field(default_factory=list)
    booking_id: str | None = None
    approval_id: str | None = None
    status_line: str | None = None


MessageBody = Annotated[
    TextBody
    | ClarifyCard
    | ShortlistCard
    | OutcomeNotice
    | ActionCard
    | CardReminder
    | OutcomeCard,
    Field(discriminator="type"),
]

KIND_OF: dict[str, Literal["text", "card", "notice"]] = {
    "text": "text",
    "clarify": "card",
    "shortlist": "card",
    "notice": "notice",
    "action": "card",
    "reminder": "card",
    "outcome": "card",
}
