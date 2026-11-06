"""The eight fixed tool contracts (architecture 5.1 and 5.2), as wire models.

This module is the contract boundary. The world builds every response through these
models and the client decodes every response through them, so a field the brief does
not list cannot appear on either side. Every model forbids extra fields and is strict:
a price sent as "1200" or a rating sent as "4.5" is a malformed response, not a
number.

Wire rules:
- times are ISO 8601 with the +05:30 offset; any other offset is rejected;
- a `date_range` is two inclusive IST calendar dates, `from <= to`, at most 14 days;
- prices are integer rupees;
- optional fields are omitted when empty, never sent as null.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from errand.clock import IST_OFFSET

MAX_RANGE_DAYS = 14
# Sent by HTTP clients on every tool call, so the world can hold a timed-out
# connection open past exactly the client's own timeout (tools/timeouts.py).
CLIENT_TIMEOUT_HEADER = "X-Errand-Client-Timeout-Ms"

ToolName = Literal[
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
]
TOOLS: tuple[ToolName, ...] = (
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
)
KEYED_TOOLS: frozenset[ToolName] = frozenset({"book", "cancel", "reschedule"})


class Service(StrEnum):
    PLUMBER = "plumber"
    ELECTRICIAN = "electrician"
    AC_SERVICING = "ac_servicing"


class Area(StrEnum):
    """Covered areas. Anything else (HSR Layout, say) is not a valid area."""

    INDIRANAGAR = "indiranagar"
    KORAMANGALA = "koramangala"
    WHITEFIELD = "whitefield"
    JAYANAGAR = "jayanagar"


def _require_ist(value: datetime) -> datetime:
    if value.utcoffset() != IST_OFFSET:
        raise ValueError("times must carry the +05:30 offset")
    return value


ISTDateTime = Annotated[datetime, AfterValidator(_require_ist)]
NonEmpty = Annotated[str, Field(min_length=1, max_length=200)]


def hold_is_live(now: datetime, expires_at: datetime) -> bool:
    """A hold is live strictly before `expires_at`; at `expires_at` it has lapsed.

    The world answers `hold_expired` exactly when this is false, and the agent uses
    the same predicate to decide whether a hold can still be booked.
    """
    return now < expires_at


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, serialize_by_alias=True)


class Slot(WireModel):
    start: ISTDateTime
    end: ISTDateTime

    @model_validator(mode="after")
    def _ordered(self) -> Slot:
        if self.end <= self.start:
            raise ValueError("slot end must be after its start")
        return self


class DateRange(WireModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    from_: date = Field(alias="from")
    to: date

    @model_validator(mode="after")
    def _bounded(self) -> DateRange:
        if self.from_ > self.to:
            raise ValueError("date_range.from is after date_range.to")
        days = (self.to - self.from_).days + 1
        if days > MAX_RANGE_DAYS:
            raise ValueError(f"date_range covers {days} days; at most {MAX_RANGE_DAYS} allowed")
        return self


class Attributes(WireModel):
    verified: bool
    years_experience: int
    languages: list[str]
    brands: list[str]


class ProviderSummary(WireModel):
    provider_id: str
    name: str
    service: Service
    area: Area
    price_inr: int
    rating: float
    review_count: int
    available_slots: list[Slot]
    attributes: Attributes


class Review(WireModel):
    review_id: str
    rating: int
    text: str
    posted_at: ISTDateTime


BookingState = Literal["confirmed", "completed", "cancelled", "cancelled_by_provider"]
StatusAnswer = Literal["confirmed", "not_found", "cancelled", "cancelled_by_provider"]


class BookingItem(WireModel):
    booking_id: str
    provider_id: str
    provider_name: str
    service: Service
    slot: Slot
    status: BookingState


# Inputs


class SearchProvidersIn(WireModel):
    service: Service
    area: Area
    date_range: DateRange


class GetProviderIn(WireModel):
    provider_id: NonEmpty


class HoldSlotIn(WireModel):
    provider_id: NonEmpty
    slot: Slot


class BookIn(WireModel):
    hold_id: NonEmpty
    idempotency_key: NonEmpty


class GetBookingStatusIn(WireModel):
    hold_id: NonEmpty | None = None
    idempotency_key: NonEmpty | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GetBookingStatusIn:
        if (self.hold_id is None) == (self.idempotency_key is None):
            raise ValueError("give exactly one of hold_id or idempotency_key")
        return self


class CancelIn(WireModel):
    booking_id: NonEmpty
    idempotency_key: NonEmpty


class RescheduleIn(WireModel):
    booking_id: NonEmpty
    slot: Slot
    idempotency_key: NonEmpty


class ListBookingsIn(WireModel):
    user_id: NonEmpty


# Success outputs


class SearchProvidersOut(WireModel):
    providers: list[ProviderSummary] = Field(min_length=3, max_length=8)


class GetProviderOut(WireModel):
    provider_id: str
    name: str
    service: Service
    area: Area
    price_inr: int
    rating: float
    attributes: Attributes
    about: str
    available_slots: list[Slot]
    reviews: list[Review]


class HoldSlotOut(WireModel):
    hold_id: str
    provider_id: str
    slot: Slot
    quoted_price_inr: int
    expires_at: ISTDateTime


class BookOut(WireModel):
    status: Literal["confirmed"]
    booking_id: str
    provider_id: str
    slot: Slot
    price_inr: int


class BookingStatusOut(WireModel):
    status: StatusAnswer
    booking_id: str | None = None
    slot: Slot | None = None


class CancelOut(WireModel):
    status: Literal["cancelled"]
    booking_id: str


class RescheduleOut(WireModel):
    status: Literal["rescheduled"]
    booking_id: str
    slot: Slot


class ListBookingsOut(WireModel):
    bookings: list[BookingItem]


# Business answers. Each tool has its own closed set, so an answer the brief does not
# allow for that tool fails decoding instead of slipping through as a generic error.


class SearchProvidersError(WireModel):
    error: Literal["invalid_args"]
    reason: str | None = None


class GetProviderError(WireModel):
    error: Literal["not_found", "invalid_args"]
    reason: str | None = None


class HoldSlotError(WireModel):
    error: Literal["invalid_args"]
    reason: str | None = None


class BookError(WireModel):
    error: Literal["slot_taken", "hold_expired", "invalid_args", "idempotency_key_reuse"]
    reason: str | None = None


class BookingStatusError(WireModel):
    error: Literal["invalid_args"]
    reason: str | None = None


class CancelError(WireModel):
    error: Literal["invalid_args", "idempotency_key_reuse"]
    reason: str | None = None


class RescheduleError(WireModel):
    error: Literal["slot_conflict", "invalid_args", "idempotency_key_reuse"]
    reason: str | None = None


class ListBookingsError(WireModel):
    error: Literal["invalid_args"]
    reason: str | None = None


class ServiceUnavailable(WireModel):
    """Body of a real HTTP 503. Transports raise on it; it is never a typed result."""

    error: Literal["service_unavailable"]
    http_status: Literal[503]
    retryable: Literal[True]


SERVICE_UNAVAILABLE = ServiceUnavailable(
    error="service_unavailable", http_status=503, retryable=True
)


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    input: type[WireModel]
    ok: type[WireModel]
    error: type[WireModel]


SPECS: dict[ToolName, ToolSpec] = {
    "search_providers": ToolSpec(
        input=SearchProvidersIn, ok=SearchProvidersOut, error=SearchProvidersError
    ),
    "get_provider": ToolSpec(input=GetProviderIn, ok=GetProviderOut, error=GetProviderError),
    "hold_slot": ToolSpec(input=HoldSlotIn, ok=HoldSlotOut, error=HoldSlotError),
    "book": ToolSpec(input=BookIn, ok=BookOut, error=BookError),
    "get_booking_status": ToolSpec(
        input=GetBookingStatusIn, ok=BookingStatusOut, error=BookingStatusError
    ),
    "cancel": ToolSpec(input=CancelIn, ok=CancelOut, error=CancelError),
    "reschedule": ToolSpec(input=RescheduleIn, ok=RescheduleOut, error=RescheduleError),
    "list_bookings": ToolSpec(input=ListBookingsIn, ok=ListBookingsOut, error=ListBookingsError),
}


def encode(model: WireModel) -> bytes:
    """The one JSON encoding of a wire model: aliases on, empty optionals omitted."""
    return model.model_dump_json(by_alias=True, exclude_none=True).encode()
