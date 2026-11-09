"""Run outcomes (15.2), what the planner loop hands to `respond` (PlanResult), and what
the code nodes on the booking path hand to it (StepResult, M4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.brief import BriefField
from errand.domain.slots import HoldRef


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
    """A propose_* that passed validation in the loop. `prepare_proposal` validates it
    again before holding, and `execute` a third time inside its guard transaction."""

    kind: Literal["proposal"] = "proposal"
    action: Literal["book", "cancel", "reschedule"]
    provider_id: str | None = None
    slot_ref: str | None = None
    booking_id: str | None = None


class OutOfCoverage(_Result):
    """update_brief's `not_covered` named a place or service we do not serve (7.3): a code
    template lists what we do cover, as quick replies."""

    kind: Literal["out_of_coverage"] = "out_of_coverage"
    field: Literal["area", "service"]
    words: str
    covered: list[str]
    # False when asking would break the ask rules (the field is filled, or its ask budget
    # is spent): the template is then a notice, not a question card.
    asked: bool = True


class NoFit(_Result):
    """A search that found nobody (10.2): a code template names what could widen."""

    kind: Literal["no_fit"] = "no_fit"
    # (kind, label, reply) for the brief's constraints, in the order window, budget, area
    widen: list[tuple[str, str, str]] = Field(default_factory=list)
    summary: str = ""


class NoAction(_Result):
    """The loop ended twice with prose that points at a selection but proposes nothing
    (10.4): MODEL_NO_PROGRESS, and the shortlist card stays on screen."""

    kind: Literal["no_action"] = "no_action"


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
    Reply
    | Ask
    | AskExhausted
    | Options
    | Proposal
    | Limit
    | ToolDown
    | TooLarge
    | Failure
    | OutOfCoverage
    | NoFit
    | NoAction,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------- booking path (M4)
#
# Results of the code nodes (prepare_proposal, execute, verify) and of code-only routing
# in ingress. Each one renders to exactly one outcome template in `respond`.


class CardReady(_Result):
    """A pending approval and its card exist; respond posts the card and parks."""

    kind: Literal["card_ready"] = "card_ready"
    approval_id: str
    # Why a card replaced an earlier one in this run, if it did (a lapsed hold at
    # preflight, a material change at execute time); respond says so above the card.
    replaced_reason: str | None = None
    # Code-written facts for that sentence (the old and new price, the moved slot).
    replaced_detail: str | None = None


class ProposalRefused(_Result):
    """prepare_proposal's validation refused (a stale list, a duplicate, a slot gone)."""

    kind: Literal["proposal_refused"] = "proposal_refused"
    reason: str
    text: str


class HoldFailed(_Result):
    kind: Literal["hold_failed"] = "hold_failed"
    provider_id: str
    slot_label: str


class OverBudget(_Result):
    """The held quote is above the budget: no approval row exists for it (8.6)."""

    kind: Literal["over_budget"] = "over_budget"
    provider_id: str
    provider_name: str
    slot_label: str
    quoted_price_inr: int
    budget_inr: int


class ConfirmHint(_Result):
    """A bare acknowledgement while the latest card allows typed grants: a code hint."""

    kind: Literal["confirm_hint"] = "confirm_hint"
    approval_id: str


class Declined(_Result):
    kind: Literal["declined"] = "declined"
    approval_id: str


class AlreadyHandled(_Result):
    """execute found the approval no longer consumable (another run consumed it)."""

    kind: Literal["already_handled"] = "already_handled"
    approval_id: str
    reason: str


class Requote(_Result):
    """execute's preflight sends the errand back for a fresh hold and a new card."""

    kind: Literal["requote"] = "requote"
    approval_id: str
    reason: Literal["hold_lapsed", "material_changed", "price_changed", "slot_changed"]
    # M5: the hold execute's re-hold already placed (9.4 step 2). The new card is built
    # on it rather than on a third hold, because the provider may have moved the slot.
    hold: HoldRef | None = None
    detail: str | None = None


class Replan(_Result):
    """prepare_proposal sends the run back to plan once, with a SystemNote."""

    kind: Literal["replan"] = "replan"
    note: Literal["hold_failed", "over_budget", "slot_taken"]


class Booked(_Result):
    kind: Literal["booked"] = "booked"
    approval_id: str
    intent_id: str
    booking_id: str
    # M5 (9.4 step 6): the provider booked it and then cancelled it before verify looked.
    # The intent still succeeded, but the user hears both facts and the outcome is not a
    # *_VERIFIED one (no status answer said `confirmed`).
    cancelled_by_provider: bool = False


class ActionFailed(_Result):
    kind: Literal["action_failed"] = "action_failed"
    approval_id: str
    reason: Literal[
        "slot_taken",
        "hold_expired",
        "provider_refused",
        "not_consumable",
        "duplicate",
        "not_booked",
    ]
    fresh_quote: bool = False


class PendingCheck(_Result):
    """The provider's answer is not settled: the intent is needs_attention."""

    kind: Literal["pending_check"] = "pending_check"
    approval_id: str
    intent_id: str
    reason: Literal[
        "status_not_confirmed",
        "material_mismatch",
        "check_incomplete",
        "fenced_off",
        "status_unavailable",
        "hold_live",
    ]
    detail: str = ""
    # hold_live (the wait rule): when the hold lapses by the world clock, ISO with +05:30.
    lapse_at: str | None = None
    # material_mismatch: the provider's booking that differs from what was approved.
    booking_id: str | None = None


class BookCalled(_Result):
    """execute handed the attempt to verify: what `book` answered, classified."""

    kind: Literal["book_called"] = "book_called"
    approval_id: str
    intent_id: str
    attempt_id: str
    answer: Literal["confirmed", "unknown"]
    booking_id: str | None = None
    price_inr: int | None = None


# ---------------------------------------------------------------- cancel and reschedule (N1)
#
# The same intent and attempt flow as a booking, with no hold (9.5). `BookCalled` is also
# what execute hands verify for these (the attempt to check); these are what comes back.


class Changed(_Result):
    """A cancel or reschedule that `list_bookings` shows: CANCELLED_VERIFIED or
    RESCHEDULED_VERIFIED."""

    kind: Literal["changed"] = "changed"
    action: Literal["cancel", "reschedule"]
    approval_id: str
    intent_id: str
    booking_id: str
    # A cancel whose booking the provider had already cancelled: the end state the user
    # asked for, reported as the provider's doing, never as ours.
    by_provider: bool = False


class SlotAlternative(_Result):
    ref: str  # the handle a select_slot click names (S1, ...)
    label: str  # "Sat 14 Nov, 10:00-11:00"
    start: str  # ISO 8601 with +05:30
    end: str


class ChangeFailed(_Result):
    """A cancel or reschedule that did not happen, and the plain reason."""

    kind: Literal["change_failed"] = "change_failed"
    action: Literal["cancel", "reschedule"]
    approval_id: str
    reason: Literal[
        "booking_changed",  # the booking is not what the card showed any more
        "still_confirmed",  # the provider could not be reached; list_bookings says unchanged
        "slot_conflict",  # a reschedule onto a slot that is taken (definitive)
        "provider_cancelled",  # a reschedule of a booking the provider cancelled
        "rejected",  # the provider refused the request
        "in_progress",  # another change to the booking got there first
    ]
    # Code-rendered buttons for the next free slots that day (slot_conflict), in order.
    alternatives: list[SlotAlternative] = Field(default_factory=list)


StepResult = Annotated[
    CardReady
    | ProposalRefused
    | HoldFailed
    | OverBudget
    | ConfirmHint
    | Declined
    | AlreadyHandled
    | Requote
    | Replan
    | Booked
    | ActionFailed
    | PendingCheck
    | BookCalled
    | Changed
    | ChangeFailed,
    Field(discriminator="kind"),
]
