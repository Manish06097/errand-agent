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

from errand.domain.brief import BriefField, ProvenanceSource
from errand.domain.material import Material
from errand.domain.outcomes import RunOutcome
from errand.domain.slots import Badge


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBody(_Body):
    type: Literal["text"] = "text"
    text: str
    status_line: str | None = None


class BriefChip(_Body):
    """One filled brief field, as a chip with where it came from (M8).

    `source` is the provenance source ("user_msg", later "memory"), `evidence` the user's
    words cut to 60 characters, and `window_label` the resolved window on the window chip
    so the user can catch a wrong reading ("Thu 12 Nov - Fri 13 Nov, 10:00-12:00").
    """

    field: BriefField
    value: str
    source: ProvenanceSource
    evidence: str | None = None
    window_label: str | None = None


class ClarifyCard(_Body):
    """A question for the fields the brief still lacks, with quick replies."""

    type: Literal["clarify"] = "clarify"
    errand_id: str
    fields: list[BriefField]
    question: str
    options: list[str] = Field(default_factory=list)
    status_line: str | None = None
    brief: list[BriefChip] = Field(default_factory=list)


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
    # The final score and its parts (10.6, rounded to 4 places in ranking): the data behind
    # "Why this order". The parts are rating, price_headroom, slot_fit, review_signal and
    # pref_match, each in [0, 1].
    score: float = 0.0
    score_parts: dict[str, float] = Field(default_factory=dict)


class ExcludedOption(_Body):
    """A provider left off the shortlist, with a code-written reason."""

    provider_id: str
    name: str
    reason: str  # over_budget, no_slot_in_window, over_cap, avoided
    reason_text: str


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
    excluded: list[ExcludedOption] = Field(default_factory=list)
    brief: list[BriefChip] = Field(default_factory=list)


class NoticeAction(_Body):
    kind: Literal[
        "retry",
        "narrow_search",
        "shorten",
        "answer",
        "refresh_hold",
        "raise_budget",
        "options",
        "check_now",
        "cancel",
        "quick_reply",
        "widen_followup",
        "dismiss_followup",
        "select_slot",
    ]
    label: str
    # For refresh_hold: the approval whose slot gets a fresh hold and a new card.
    approval_id: str | None = None
    # For quick_reply: the message the button sends as if the user had typed it.
    reply: str | None = None
    # For select_slot (a follow-up's next candidates): the shortlist handle to hold.
    slot_ref: str | None = None
    # For widen_followup: which constraint one tap widens, and the rupee ceiling of "Up to".
    field: Literal["days", "price", "time"] | None = None
    value: str | None = None


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
    # select_slot buttons a failed reschedule offers: the next free slots that day (N1).
    alternatives: list[SlotOption] = Field(default_factory=list)
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
    # A reschedule's other eligible slots (N1): each is a select_slot button that cards
    # that slot instead. Empty on a booking or a cancel.
    alternatives: list[SlotOption] = Field(default_factory=list)
    status_line: str | None = None
    brief: list[BriefChip] = Field(default_factory=list)
    # A Rebook (14.5): "Replaces B-12, cancelled by Ravi Plumbing", and how its price
    # compares with the cancelled booking's. Absent on a plain book.
    header: str | None = None


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


class ProactiveLine(_Body):
    """One line the agent writes into a conversation its run does not own (14.2, INV-15): the
    origin chat hears about a cancel and, later, a rebooking. It carries no approval and no
    question; the typed-grant binding looks past it."""

    type: Literal["proactive"] = "proactive"
    text: str
    followup_conversation_id: str
    booking_id: str
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


class MemorySavedChip(_Body):
    """ "Saved: home area" with Undo. References the row; the value is resolved at read time."""

    type: Literal["memory_saved"] = "memory_saved"
    memory_id: str


class MemorySuggestionChip(_Body):
    """One per errand that produced a suggestion: Keep and Dismiss."""

    type: Literal["memory_suggestion"] = "memory_suggestion"
    memory_id: str


class MemoryOfferChip(_Body):
    """ "Remember this for next time?" It stores no value, only where the user said it: a
    click re-reads the span out of the user's message, so a forgotten value has no copy."""

    type: Literal["memory_offer"] = "memory_offer"
    key: str
    scope: str
    source_message_id: str
    start: int
    end: int


class MemoryUpdateChip(_Body):
    """ "Update your saved home area? [Update] [Just this time]" with the same no-value rule."""

    type: Literal["memory_update"] = "memory_update"
    memory_id: str
    key: str
    scope: str
    source_message_id: str
    start: int
    end: int


class MemoryForgetChip(_Body):
    """What `propose_forget` renders. The forget is the user's click, never the model's."""

    type: Literal["memory_forget"] = "memory_forget"
    memory_id: str


class MemoryListCard(_Body):
    """What `list_memories` renders: references only, resolved with their status at read time."""

    type: Literal["memory_list"] = "memory_list"
    memory_ids: list[str] = Field(default_factory=list)
    suggestion_ids: list[str] = Field(default_factory=list)


MessageBody = Annotated[
    TextBody
    | ClarifyCard
    | ShortlistCard
    | OutcomeNotice
    | ActionCard
    | CardReminder
    | ProactiveLine
    | OutcomeCard
    | MemorySavedChip
    | MemorySuggestionChip
    | MemoryOfferChip
    | MemoryUpdateChip
    | MemoryForgetChip
    | MemoryListCard,
    Field(discriminator="type"),
]

MEMORY_BODY_TYPES = frozenset(
    {
        "memory_saved",
        "memory_suggestion",
        "memory_offer",
        "memory_update",
        "memory_forget",
        "memory_list",
    }
)

KIND_OF: dict[str, Literal["text", "card", "notice", "proactive"]] = {
    "proactive": "proactive",
    "text": "text",
    "clarify": "card",
    "shortlist": "card",
    "notice": "notice",
    "action": "card",
    "reminder": "card",
    "outcome": "card",
    "memory_saved": "card",
    "memory_suggestion": "card",
    "memory_offer": "card",
    "memory_update": "card",
    "memory_forget": "card",
    "memory_list": "card",
}
