"""ScenarioConfig: the YAML that sets up one world (architecture 5.5).

Two blocks are kept apart on purpose:
- `world` holds only on-contract behaviour (dedupe, forced outcomes, the outcome
  split, quote drift, hold TTL, reschedule conflicts, injection reviews, scheduled
  provider cancellations, latency);
- `faults` holds the test-only failures and is empty by default. `fault_names()` is
  what the eval table's `world` column shows.

Every block forbids unknown keys, so a typo'd fault fails loudly instead of quietly
running the default world.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from errand.clock import IST
from errand.world.contracts import TOOLS, BookingState, ISTDateTime, Service, ToolName
from errand.world.rng import BookOutcome

_DURATION = re.compile(r"^([+-]?)(\d+)(ms|s|m|h|d)$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_UPCOMING = re.compile(r"^upcoming:(\d+)$")
_CREATED = re.compile(r"^created:(\d+)$")


def parse_duration(text: str) -> timedelta:
    """'+30m', '-1h', '90s', '500ms', '2d'."""
    match = _DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (use forms like +30m, 90s, 2h)")
    sign = -1.0 if match.group(1) == "-" else 1.0
    return timedelta(seconds=sign * int(match.group(2)) * _UNITS[match.group(3)])


def parse_conflict_slot(text: str) -> tuple[str, datetime]:
    """'P2@2026-11-14T10:00+05:30' -> ('P2', datetime)."""
    provider, sep, when = text.partition("@")
    if not sep or not provider:
        raise ValueError(f"conflict slot {text!r} is not PROVIDER@ISO_START")
    start = datetime.fromisoformat(when)
    if start.utcoffset() != timedelta(hours=5, minutes=30):
        raise ValueError(f"conflict slot {text!r} must use the +05:30 offset")
    return provider, start


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedBooking(_Block):
    provider: str
    service: Service
    slot: ISTDateTime = Field(description="slot start; seeded slots last one hour")
    status: BookingState


def _default_bookings() -> list[SeedBooking]:
    return [
        # "same electrician as last time"
        SeedBooking(
            provider="E4",
            service=Service.ELECTRICIAN,
            slot=datetime(2026, 10, 21, 10, 0, tzinfo=IST),
            status="completed",
        ),
        # "move my AC servicing"
        SeedBooking(
            provider="A2",
            service=Service.AC_SERVICING,
            slot=datetime(2026, 11, 12, 11, 0, tzinfo=IST),
            status="confirmed",
        ),
    ]


class UserConfig(_Block):
    user_id: str = "u_demo"
    bookings: list[SeedBooking] = Field(default_factory=_default_bookings)


class BookConfig(_Block):
    forced: list[BookOutcome] = Field(default_factory=list)


class BookProbabilities(_Block):
    success: float = 0.70
    slot_taken: float = 0.15
    timeout_booked: float = 0.075
    timeout_not_booked: float = 0.075

    @model_validator(mode="after")
    def _sums_to_one(self) -> BookProbabilities:
        values = self.as_dict()
        if any(not 0.0 <= v <= 1.0 for v in values.values()):
            raise ValueError("each book probability must be within [0, 1]")
        total = math.fsum(values.values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"book probabilities sum to {total}, not 1")
        return self

    def as_dict(self) -> dict[BookOutcome, float]:
        return {
            "success": self.success,
            "slot_taken": self.slot_taken,
            "timeout_booked": self.timeout_booked,
            "timeout_not_booked": self.timeout_not_booked,
        }


class QuoteDrift(_Block):
    """How `hold_slot`'s quote differs from the search price.

    - `on_hold`: every hold of a listed provider adds its delta.
    - `on_rehold`: only the second and later holds of the same slot add it.
    - `random_pct`: a keyed draw moves every quote by up to `pct` percent either way.
    """

    mode: Literal["none", "on_hold", "on_rehold", "random_pct"] = "none"
    providers: dict[str, int] = Field(default_factory=dict)
    pct: float | None = None

    @model_validator(mode="after")
    def _consistent(self) -> QuoteDrift:
        if self.mode in ("on_hold", "on_rehold") and not self.providers:
            raise ValueError(f"quote_drift {self.mode} needs a providers map")
        if self.mode == "random_pct" and (self.pct is None or not 0 < self.pct <= 100):
            raise ValueError("quote_drift random_pct needs pct in (0, 100]")
        if self.mode == "none" and (self.providers or self.pct is not None):
            raise ValueError("quote_drift none takes no providers or pct")
        return self


class EventConfig(_Block):
    """A scheduled provider cancellation.

    - `upcoming:N`: the Nth upcoming seeded booking; `fire_at` is a duration after
      `clock_start` or an ISO time.
    - `created:N`: the Nth booking the world creates in this scenario (0-based, seeded
      history excluded); `fire_at` is a duration after that booking was created, so a
      scenario can have the provider pull out of a booking the agent made.
    """

    type: Literal["provider_cancelled"]
    booking: str = Field(
        description="upcoming:N, the Nth upcoming seeded booking, or created:N, the Nth "
        "booking the world creates"
    )
    fire_at: str = Field(
        description="a duration after clock_start (+30m) or an ISO time; after the "
        "booking's creation for created:N"
    )

    @field_validator("booking")
    @classmethod
    def _booking_ref(cls, value: str) -> str:
        if _UPCOMING.match(value) is None and _CREATED.match(value) is None:
            raise ValueError(f"event booking {value!r} must look like upcoming:0 or created:0")
        return value

    @model_validator(mode="after")
    def _created_is_relative(self) -> EventConfig:
        if self.created_index() is not None and _DURATION.match(self.fire_at.strip()) is None:
            raise ValueError("an event on created:N fires a duration after the booking (+5s)")
        return self

    @field_validator("fire_at")
    @classmethod
    def _fire_at(cls, value: str) -> str:
        if _DURATION.match(value.strip()):
            return value
        when = datetime.fromisoformat(value)
        if when.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError("event fire_at must be a duration or a +05:30 time")
        return value

    def upcoming_index(self) -> int:
        match = _UPCOMING.match(self.booking)
        assert match is not None
        return int(match.group(1))

    def created_index(self) -> int | None:
        match = _CREATED.match(self.booking)
        return int(match.group(1)) if match else None

    def resolve_fire_at(self, clock_start: datetime) -> datetime:
        if _DURATION.match(self.fire_at.strip()):
            return clock_start + parse_duration(self.fire_at)
        return datetime.fromisoformat(self.fire_at)


class InjectionExtra(_Block):
    provider: str
    text: str
    rating: int = Field(5, ge=1, le=5)


class WorldConfig(_Block):
    enforce_idempotency: bool = True
    book: BookConfig = Field(default_factory=BookConfig)
    book_probabilities: BookProbabilities = Field(default_factory=BookProbabilities)
    quote_drift: QuoteDrift = Field(default_factory=QuoteDrift)
    hold_ttl_s: int = Field(600, gt=0)
    reschedule_conflict_slots: list[str] = Field(default_factory=list)
    # "default" seeds the template's injection reviews; a list adds extras to them.
    # "clean" is the eval twin: each template injection review becomes a clean review
    # with the same id, provider and rating, so a rank comparison is like with like.
    injection_reviews: Literal["default", "clean"] | list[InjectionExtra] = "default"
    events: list[EventConfig] = Field(default_factory=list)
    latency_ms: dict[str, int] = Field(default_factory=dict)

    @field_validator("reschedule_conflict_slots")
    @classmethod
    def _conflicts(cls, value: list[str]) -> list[str]:
        for item in value:
            parse_conflict_slot(item)
        return value

    @field_validator("latency_ms")
    @classmethod
    def _latency(cls, value: dict[str, int]) -> dict[str, int]:
        allowed = {"default", *TOOLS}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown latency_ms keys {unknown}; use default or a tool name")
        if any(v < 0 for v in value.values()):
            raise ValueError("latency_ms values must be >= 0")
        return value

    def latency_s(self, tool: ToolName) -> float:
        return self.latency_ms.get(tool, self.latency_ms.get("default", 0)) / 1000

    def conflict_slots(self) -> set[tuple[str, datetime]]:
        return {parse_conflict_slot(s) for s in self.reschedule_conflict_slots}


class MalformedFault(_Block):
    tool: ToolName
    mode: Literal["bad_json", "extra_fields", "wrong_types"]


class StatusOutage(_Block):
    unavailable_n: int = Field(gt=0)


class CommitLatency(_Block):
    book: int = Field(gt=0)


class FaultsConfig(_Block):
    """Test-only failures. None of them is part of the brief's contract."""

    cancel: Literal["always_503"] | None = None
    get_booking_status: StatusOutage | None = None
    search_providers: Literal[503] | None = None
    get_provider: Literal[503] | None = None
    malformed: MalformedFault | None = None
    commit_latency_ms: CommitLatency | None = None
    rehold_slot_shift: dict[str, str] | None = None
    book_price_mismatch: int | None = None
    duplicate_delivery: Literal["book", "cancel", "reschedule"] | None = None

    @field_validator("rehold_slot_shift")
    @classmethod
    def _shifts(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        for shift in (value or {}).values():
            parse_duration(shift)
        return value

    def fault_names(self) -> list[str]:
        return [name for name in type(self).model_fields if getattr(self, name) is not None]

    def slot_shift(self, provider_id: str) -> timedelta | None:
        if not self.rehold_slot_shift or provider_id not in self.rehold_slot_shift:
            return None
        return parse_duration(self.rehold_slot_shift[provider_id])


class ScenarioConfig(_Block):
    id: str
    seed: int = 0
    clock_start: ISTDateTime = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
    catalog: Literal["bangalore_v1"] = "bangalore_v1"
    user: UserConfig = Field(default_factory=UserConfig)
    world: WorldConfig = Field(default_factory=WorldConfig)
    faults: FaultsConfig = Field(default_factory=FaultsConfig)

    def fault_names(self) -> list[str]:
        return self.faults.fault_names()

    @classmethod
    def from_yaml(cls, text: str) -> ScenarioConfig:
        data: Any = yaml.load(text, Loader=_NoTimestampLoader)
        if not isinstance(data, dict):
            raise ValueError("a scenario is a YAML mapping")
        return cls.model_validate(data)

    @classmethod
    def from_file(cls, path: Path) -> ScenarioConfig:
        return cls.from_yaml(path.read_text(encoding="utf-8"))


class _NoTimestampLoader(yaml.SafeLoader):
    """SafeLoader that leaves timestamps as strings.

    PyYAML would turn an unquoted ISO time into a datetime and drop or convert the
    offset; the models validate the +05:30 offset themselves.
    """


_NoTimestampLoader.yaml_implicit_resolvers = {
    key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
