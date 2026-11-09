"""S5: older history as lines built by code from records (13.3, gate 2 decision 20).

No model ever summarises. A line carries booking-record fields only (when, service,
provider, status), never a brief value, which is why forgetting or correcting a memory
needs no rewrite of the ledger and why "same electrician as last time" still finds E4
after a forget (decision 12). Newest first; what does not fit becomes
"and N earlier errands".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo

from errand.clock import from_ms
from errand.llm.tokens import est_tokens
from errand.store.repos.known_bookings import KnownBooking

S5_CAP = 400
HEAD = "<history>\n"
TAIL = "\n</history>"
SHOWN_STATUSES = frozenset({"confirmed", "completed", "cancelled"})


@dataclass(frozen=True)
class Ledger:
    text: str  # "" when there is no history
    lines: int  # booking lines shown
    older: int  # errands folded into the overflow line
    tokens: int


def line_for(b: KnownBooking, tz: tzinfo, names: Mapping[str, str] | None = None) -> str:
    when = from_ms(b.slot_start).astimezone(tz)
    provider = (names or {}).get(b.provider_id, b.provider_id)
    return (
        f"{when.day} {when:%b}: {b.service.replace('_', ' ')} with {provider}, "
        f"{when:%a %H:%M}, {b.status}"
    )


def build_ledger(
    bookings: Sequence[KnownBooking],
    *,
    tz: tzinfo,
    names: Mapping[str, str] | None = None,
    cap_tokens: int = S5_CAP,
    exclude: frozenset[str] = frozenset(),
) -> Ledger:
    """`exclude` holds booking ids already shown elsewhere (S3's upcoming bookings)."""
    rows = sorted(
        (b for b in bookings if b.status in SHOWN_STATUSES and b.booking_id not in exclude),
        key=lambda b: (-b.slot_start, b.booking_id),
    )
    if not rows:
        return Ledger("", 0, 0, 0)
    shown: list[str] = []
    for index, b in enumerate(rows):
        candidate = [*shown, line_for(b, tz, names)]
        remaining = len(rows) - index - 1
        overflow = [f"and {remaining} earlier errands"] if remaining else []
        if est_tokens(HEAD + "\n".join(candidate + overflow) + TAIL) > cap_tokens:
            break
        shown = candidate
    older = len(rows) - len(shown)
    lines = [*shown, f"and {older} earlier errands"] if older else shown
    if not lines:  # not even one line fits: say only how many there are
        lines = [f"and {older} earlier errands"]
    text = HEAD + "\n".join(lines) + TAIL
    return Ledger(text, len(shown), older, est_tokens(text))
