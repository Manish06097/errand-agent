"""Booking handles for the planner's `list_bookings` view (N1), like slot handles.

The planner never writes a booking id. `list_bookings` gives each booking a handle
(`B1`, `B2`, ...), assigned by code in one stable order (slot start, then booking id), and
`propose_cancel` and `propose_reschedule` name a booking by that handle. A handle is valid
only for the view issued in the same run: the loop keeps the mapping it rendered, and a new
run starts with none, so a handle can never point at a booking from an earlier listing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from errand.world.contracts import BookingItem

BOOKING_REF = re.compile(r"^B[1-9][0-9]{0,2}$")


def assign(bookings: Sequence[BookingItem], limit: int) -> dict[str, BookingItem]:
    """`{"B1": booking, ...}` for the first `limit` bookings in the stable order."""
    ordered = sorted(bookings, key=lambda b: (b.slot.start, b.booking_id))
    return {f"B{i}": b for i, b in enumerate(ordered[:limit], start=1)}
