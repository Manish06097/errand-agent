"""The brief: what the user wants, field by field, each with its provenance (7.1, 7.3)."""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.world.contracts import Area, Service


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefField(StrEnum):
    SERVICE = "service"
    ISSUE = "issue"
    AREA = "area"
    WINDOW = "window"
    BUDGET = "budget_max_inr"
    PROVIDER_PREF = "provider_pref"
    TARGET_BOOKING = "target_booking_id"
    NEW_WINDOW = "new_window"


class ErrandKind(StrEnum):
    BOOK = "book"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"
    REBOOK = "rebook"


ProvenanceSource = Literal[
    "user_msg", "ui_action", "memory", "booking_history", "followup", "default"
]


class Provenance(_Model):
    source: ProvenanceSource
    message_id: str | None = None
    memory_id: str | None = None
    booking_id: str | None = None
    # The exact user substring the code checked; shown to the model cut to 60 chars and
    # dropped on forget (M11).
    evidence: str | None = None


PartOfDay = Literal["morning", "afternoon", "evening", "any"]


class TimeWindowExpr(_Model):
    """What the model extracts. It never carries a date: code resolves the phrase
    against the clock (10.5). Weekday names arrive with the full resolver in M8."""

    phrase: str
    part_of_day: PartOfDay | None = None
    not_before: time | None = None
    not_after: time | None = None


class TimeWindow(_Model):
    """What code resolves (`agent.timewin.resolve`). `end` is exclusive."""

    start: datetime
    end: datetime
    daily_from: time | None = None
    daily_to: time | None = None
    weekdays: frozenset[int] | None = None  # ISO weekdays, 1 = Monday
    label: str


class ProviderPref(_Model):
    prefer_id: str | None = None
    avoid_ids: list[str] = Field(default_factory=list)


class _Fact(_Model):
    prov: Provenance
    set_at_turn: int


# Concrete fact types, one per value type: a parametrised generic is not restored by
# a strict-msgpack checkpoint (it comes back as a dict), see errand.domain.


class ServiceFact(_Fact):
    value: Service


class AreaFact(_Fact):
    value: Area


class TextFact(_Fact):
    value: str


class WindowFact(_Fact):
    value: TimeWindow
    expr: TimeWindowExpr | None = None
    # The memory row behind each time bound code applied ("not_before" / "not_after"),
    # kept apart from `prov`, which stays the user's own day: a forgotten bound drops the
    # bound and keeps the day.
    bound_memory_ids: dict[str, str] = Field(default_factory=dict)


class BudgetFact(_Fact):
    value: int | Literal["none"]


class ProviderPrefFact(_Fact):
    value: ProviderPref
    # The memory row behind each component, so forgetting one clears only that one.
    prefer_memory_id: str | None = None
    avoid_memory_id: str | None = None


class AskRecord(_Model):
    """One field asked in one ClarifyCard."""

    field: BriefField
    turn: int
    with_options: bool


class Brief(_Model):
    version: int = 0
    service: ServiceFact | None = None
    issue: TextFact | None = None  # at most 80 characters
    area: AreaFact | None = None
    window: WindowFact | None = None
    budget_max_inr: BudgetFact | None = None
    provider_pref: ProviderPrefFact | None = None
    target_booking_id: TextFact | None = None  # cancel, reschedule, rebook
    new_window: WindowFact | None = None  # reschedule target
    # Every ask that reached the user, in order: at most two per field (ask, re-ask).
    asks: list[AskRecord] = Field(default_factory=list)
    # Fields whose ask budget is spent; never asked again in this errand.
    ask_exhausted: list[BriefField] = Field(default_factory=list)
    # Fields the user cleared in this errand: memory does not refill them (decision 17).
    user_cleared: list[BriefField] = Field(default_factory=list)
