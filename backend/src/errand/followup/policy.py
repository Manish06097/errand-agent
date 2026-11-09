"""The notify policy (architecture 14.4, Gate 3 decision 3): a pure function.

Code gathers the facts (`Facts`), `decide` returns what to do, first match wins. Nothing
is persisted for the facts: "a cancel or reschedule card was pending or granted" is read
back by the caller from approvals invalidated with `booking_no_longer_active`, so a job
resumed after a crash decides the same way.

    1. the slot has started, or a replacement for it already succeeded -> ignored (S41)
    2. another confirmed booking of the same service is already inside the replacement
       window -> notify, no search (S41b)
    3. a cancel card for it was pending or granted -> notify "nothing for you to cancel",
       no offer
    4. a reschedule card for it was pending or granted -> notify, search the window the
       user was moving to
    5. the slot is within 24 h -> urgent
    6. otherwise -> normal

Urgency only describes a notice that has something to act on (rows 4 to 6). Rows 2 and 3
are information, so they are never urgent: a toast and a banner for "nothing for you to
cancel" would be noise (a reading of the table the lead can overrule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

URGENT_WITHIN = timedelta(hours=24)


class Action(StrEnum):
    IGNORE = "ignore"
    NOTIFY_NO_SEARCH = "notify_no_search"
    NOTIFY_NOTHING_TO_CANCEL = "notify_nothing_to_cancel"
    NOTIFY_SEARCH = "notify_search"


class Urgency(StrEnum):
    NORMAL = "normal"
    URGENT = "urgent"


class IgnoreReason(StrEnum):
    SLOT_STARTED = "slot_started"
    ALREADY_REPLACED = "already_replaced"


@dataclass(frozen=True)
class Facts:
    slot_start: datetime
    now: datetime
    replacement_succeeded: bool = False
    # A confirmed booking of the same service that starts inside the replacement window
    # (its id), if there is one.
    same_service_in_window: str | None = None
    cancel_card: bool = False
    reschedule_card: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    urgency: Urgency = Urgency.NORMAL
    ignore_reason: IgnoreReason | None = None
    # NOTIFY_NO_SEARCH: the booking that made a search pointless.
    existing_booking_id: str | None = None
    # NOTIFY_SEARCH after a reschedule card: search the window it was moving to.
    use_reschedule_window: bool = False

    @property
    def notifies(self) -> bool:
        return self.action is not Action.IGNORE

    @property
    def searches(self) -> bool:
        return self.action is Action.NOTIFY_SEARCH


def decide(facts: Facts) -> Decision:
    if facts.slot_start <= facts.now:
        return Decision(Action.IGNORE, ignore_reason=IgnoreReason.SLOT_STARTED)
    if facts.replacement_succeeded:
        return Decision(Action.IGNORE, ignore_reason=IgnoreReason.ALREADY_REPLACED)
    if facts.same_service_in_window is not None:
        return Decision(Action.NOTIFY_NO_SEARCH, existing_booking_id=facts.same_service_in_window)
    if facts.cancel_card:
        return Decision(Action.NOTIFY_NOTHING_TO_CANCEL)
    urgent = facts.slot_start - facts.now <= URGENT_WITHIN
    urgency = Urgency.URGENT if urgent else Urgency.NORMAL
    if facts.reschedule_card:
        return Decision(Action.NOTIFY_SEARCH, urgency, use_reschedule_window=True)
    return Decision(Action.NOTIFY_SEARCH, urgency)
