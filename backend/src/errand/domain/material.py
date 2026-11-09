"""Material details: what an approval is bound to (architecture 8.1, ADR 0002).

One frozen model per action, schema version `v=1`. The fingerprint is a hash of the
canonical JSON of exactly these fields, so anything that should invalidate an approval
when it changes has to be a field here, and nothing else may be:

- book: action, user, provider, service, slot start and end, the **held** quote,
  `replaces_booking_id` (only on a Rebook; absent from the JSON otherwise) and
  `same_service_nearby`, the sorted ids of the user's confirmed bookings of the same
  service within a day of the slot (the card shows them, so the click covers them);
- cancel: action, user, booking, provider, slot start and the booking's status;
- reschedule: action, user, booking, provider, the old start, the new slot and the
  booking's status.

`hold_id` is deliberately absent. It is stored on the approval as `bound_hold_id`, so a
re-hold with identical material keeps the approval valid (9.3).

Normalisation lives in the validators, so equivalent inputs give equal models: times
become `+05:30` ISO strings with seconds (a UTC datetime and its IST twin are the same
time), money is a strict integer (no floats, no numeric strings, no bools), and the
nearby ids are de-duplicated and sorted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    model_serializer,
)

from errand.clock import IST
from errand.world.contracts import Service

MATERIAL_VERSION = 1
# "Within one day of the slot" for same_service_nearby: the other booking starts no
# more than 24 hours before or after this one.
NEARBY_WINDOW = timedelta(hours=24)


def ist_seconds(value: Any) -> str:
    """Any aware datetime or ISO string, as `YYYY-MM-DDTHH:MM:SS+05:30`."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime or ISO string, got {type(value).__name__}")
    if value.tzinfo is None:
        raise ValueError("material times must carry an offset")
    return value.astimezone(IST).replace(microsecond=0).isoformat(timespec="seconds")


def _sorted_ids(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, list | tuple | set | frozenset):
        raise TypeError("same_service_nearby is a list of booking ids")
    return sorted({str(v) for v in value})


IstTime = Annotated[str, BeforeValidator(ist_seconds)]
BookingIds = Annotated[list[str], BeforeValidator(_sorted_ids)]


class _Material(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1  # MATERIAL_VERSION
    user_id: str = Field(min_length=1)


class BookMaterial(_Material):
    action: Literal["book"] = "book"
    provider_id: str = Field(min_length=1)
    service: Service
    slot_start: IstTime
    slot_end: IstTime
    quoted_price_inr: StrictInt
    same_service_nearby: BookingIds = Field(default_factory=list)
    # A Rebook (14): the cancelled booking this one replaces. Left out of the canonical
    # JSON when None, so a plain book's fingerprint is exactly what it was before.
    replaces_booking_id: str | None = Field(default=None, min_length=1)

    @model_serializer(mode="wrap")
    def _omit_replaces_when_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("replaces_booking_id") is None:
            data.pop("replaces_booking_id", None)
        return data


class CancelMaterial(_Material):
    action: Literal["cancel"] = "cancel"
    booking_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    slot_start: IstTime
    booking_status: Literal["confirmed"] = "confirmed"


class RescheduleMaterial(_Material):
    action: Literal["reschedule"] = "reschedule"
    booking_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    from_start: IstTime
    to_start: IstTime
    to_end: IstTime
    booking_status: Literal["confirmed"] = "confirmed"


Material = Annotated[
    BookMaterial | CancelMaterial | RescheduleMaterial, Field(discriminator="action")
]


def is_nearby(slot_start: datetime, other_start: datetime) -> bool:
    return abs(other_start - slot_start) <= NEARBY_WINDOW
