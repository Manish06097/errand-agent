"""The two inference rules, pure over a user's booking records (gate 2 decision 4).

A rule only proposes. `MemoryService.suggest` applies the gate's other conditions (no
active row for the key or its pair, not blocklisted, not already pending) and writes a
`pending_confirmation` row whose evidence is the booking ids. A pending row never
reaches the model; only the user's Keep makes it active.

- `latest_time_before_noon`: 3 of the user's last 4 bookings start before 12:00 local
  time, so suggest `latest_time` 12:00 (global).
- `same_provider_twice`: the user has booked the same provider twice or more for a
  service, so suggest `preferred_provider` for that service. With several candidates the
  most booked wins, then the most recent.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time, tzinfo
from typing import Any

from errand.clock import from_ms
from errand.store.repos.known_bookings import KnownBooking

RULE_LATEST = "latest_time_before_noon"
RULE_PROVIDER = "same_provider_twice"
COUNTED = frozenset({"confirmed", "completed"})
NOON = time(12, 0)
WINDOW = 4
NEEDED = 3


@dataclass(frozen=True)
class Candidate:
    rule: str
    key: str
    scope: str
    value: Any
    booking_ids: tuple[str, ...]


def counted(bookings: Sequence[KnownBooking]) -> list[KnownBooking]:
    """Bookings that happened or will: not cancelled or failed. Oldest first."""
    kept = [b for b in bookings if b.status in COUNTED]
    return sorted(kept, key=lambda b: (b.slot_start, b.booking_id))


def latest_time_before_noon(bookings: Sequence[KnownBooking], tz: tzinfo) -> Candidate | None:
    last = counted(bookings)[-WINDOW:]
    if len(last) < WINDOW:
        return None
    early = [b for b in last if from_ms(b.slot_start).astimezone(tz).time() < NOON]
    if len(early) < NEEDED:
        return None
    return Candidate(
        RULE_LATEST, "latest_time", "global", "12:00", tuple(b.booking_id for b in early)
    )


def same_provider_twice(bookings: Sequence[KnownBooking]) -> list[Candidate]:
    by_service: dict[str, list[KnownBooking]] = defaultdict(list)
    for b in counted(bookings):
        by_service[b.service].append(b)
    out: list[Candidate] = []
    for service in sorted(by_service):
        rows = by_service[service]
        tally = Counter(b.provider_id for b in rows)
        repeated = [p for p, n in tally.items() if n >= 2]
        if not repeated:
            continue
        last_seen = {p: max(b.slot_start for b in rows if b.provider_id == p) for p in repeated}
        best = max(repeated, key=lambda p: (tally[p], last_seen[p], p))
        ids = tuple(b.booking_id for b in rows if b.provider_id == best)
        out.append(Candidate(RULE_PROVIDER, "preferred_provider", f"service:{service}", best, ids))
    return out


def candidates(bookings: Sequence[KnownBooking], tz: tzinfo) -> list[Candidate]:
    found: list[Candidate] = []
    latest = latest_time_before_noon(bookings, tz)
    if latest is not None:
        found.append(latest)
    found += same_provider_twice(bookings)
    return found
