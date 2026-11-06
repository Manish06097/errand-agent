"""Message bodies as stored in `messages.body_json` and served by the API.

This is the single definition of what the user sees; the web types are generated from
it in M7. `kind` on the messages row follows the body: text, card or notice.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.brief import BriefField
from errand.domain.outcomes import RunOutcome


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBody(_Body):
    type: Literal["text"] = "text"
    text: str


class ClarifyCard(_Body):
    """A question for the fields the brief still lacks, with quick replies."""

    type: Literal["clarify"] = "clarify"
    errand_id: str
    fields: list[BriefField]
    question: str
    options: list[str] = Field(default_factory=list)


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


class ShortlistCard(_Body):
    """ProviderComparisonCard data: candidates in code order, slot buttons by handle."""

    type: Literal["shortlist"] = "shortlist"
    errand_id: str
    brief_version: int
    window_label: str
    candidates: list[CandidateOption]
    excluded_count: int = 0
    recommendation: str | None = None


class NoticeAction(_Body):
    kind: Literal["retry", "narrow_search", "shorten", "answer"]
    label: str


class OutcomeNotice(_Body):
    """A code-written outcome message: limits, outages, errors, a spent ask budget."""

    type: Literal["notice"] = "notice"
    outcome: RunOutcome
    text: str
    actions: list[NoticeAction] = Field(default_factory=list)
    still_needed: list[BriefField] = Field(default_factory=list)


MessageBody = Annotated[
    TextBody | ClarifyCard | ShortlistCard | OutcomeNotice, Field(discriminator="type")
]

KIND_OF: dict[str, Literal["text", "card", "notice"]] = {
    "text": "text",
    "clarify": "card",
    "shortlist": "card",
    "notice": "notice",
}
