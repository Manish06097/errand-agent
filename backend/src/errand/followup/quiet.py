"""The contact window (Gate 3 decision 7): when a notice may be delivered. Pure.

The user's saved `contact_window` (`{from, to}` in the zone of the app, `from` before `to`)
is the only quiet rule. With none saved nothing is ever deferred (decision 2 of the gate).
Inside the window a notice is delivered as usual. Outside it a normal notice is deferred to
the window's next opening, and an urgent one is delivered at once with the badge only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from errand.memory.registry import RegistryError, spec_for
from errand.store.repos.memories import MemoryRow


@dataclass(frozen=True)
class ContactWindow:
    start: time
    end: time

    def contains(self, now: datetime, tz: tzinfo) -> bool:
        return self.start <= now.astimezone(tz).time().replace(tzinfo=None) < self.end

    def next_opening(self, now: datetime, tz: tzinfo) -> datetime:
        """The first moment at or after `now` the window is open."""
        local = now.astimezone(tz)
        today = datetime.combine(local.date(), self.start, tzinfo=tz)
        if local < today:
            return today
        if self.contains(now, tz):
            return local
        return today + timedelta(days=1)


def saved(rows: Iterable[MemoryRow]) -> ContactWindow | None:
    """The user's active global `contact_window`, or None when they have saved none."""
    for row in rows:
        if row.key != "contact_window" or row.status != "active" or row.scope != "global":
            continue
        try:
            value = spec_for(row.key).parse(row.value)
            return ContactWindow(time.fromisoformat(value["from"]), time.fromisoformat(value["to"]))
        except (RegistryError, KeyError, ValueError, TypeError):
            return None
    return None
