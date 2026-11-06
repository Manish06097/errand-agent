"""Run outcomes (15.2) and what the planner loop hands to `respond` (PlanResult)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.brief import BriefField


class RunOutcome(StrEnum):
    REPLIED = "REPLIED"
    ASKED_USER = "ASKED_USER"
    PRESENTED_OPTIONS = "PRESENTED_OPTIONS"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    BOOKED_VERIFIED = "BOOKED_VERIFIED"
    CANCELLED_VERIFIED = "CANCELLED_VERIFIED"
    RESCHEDULED_VERIFIED = "RESCHEDULED_VERIFIED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    ACTION_FAILED = "ACTION_FAILED"
    DECLINED = "DECLINED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    NOTIFIED = "NOTIFIED"
    LIMIT_REACHED = "LIMIT_REACHED"
    WALL_CLOCK_EXCEEDED = "WALL_CLOCK_EXCEEDED"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_NO_PROGRESS = "MODEL_NO_PROGRESS"
    MODEL_REFUSED = "MODEL_REFUSED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reply(_Result):
    kind: Literal["reply"] = "reply"
    text: str


class Ask(_Result):
    """An ask_user the guard let through: becomes a ClarifyCard."""

    kind: Literal["ask"] = "ask"
    fields: list[BriefField]
    question: str
    options: list[str] = Field(default_factory=list)


class AskExhausted(_Result):
    """The guard refused an ask whose budget is spent: a code message, not a card."""

    kind: Literal["ask_exhausted"] = "ask_exhausted"
    still_needed: list[BriefField]


class Options(_Result):
    """A shortlist built in this run, with the model's one-line recommendation."""

    kind: Literal["options"] = "options"
    recommendation: str | None = None


class Proposal(_Result):
    """A validated propose_* (M4)."""

    kind: Literal["proposal"] = "proposal"
    action: Literal["book", "cancel", "reschedule"]
    provider_id: str | None = None
    slot_ref: str | None = None
    booking_id: str | None = None


LimitName = Literal["model_calls", "iterations", "tool_calls", "deadline"]


class Limit(_Result):
    kind: Literal["limit"] = "limit"
    limit: LimitName


class ToolDown(_Result):
    kind: Literal["tool_down"] = "tool_down"
    tool: str
    error_class: str


class TooLarge(_Result):
    kind: Literal["too_large"] = "too_large"
    section: str
    estimated: int
    cap: int


class Failure(_Result):
    kind: Literal["failure"] = "failure"
    reason: str


PlanResult = Annotated[
    Reply | Ask | AskExhausted | Options | Proposal | Limit | ToolDown | TooLarge | Failure,
    Field(discriminator="kind"),
]
