"""Follow-up message templates (architecture 14.5): code over structured facts, no model.

Every line says only what the records say: the provider that cancelled, the booking id and
slot, what was searched, until when a hold is kept. Nothing here claims a booking.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.domain.windows import fmt_day


def when(start: datetime, tz: tzinfo, *, now: datetime | None = None) -> str:
    """ "Thu 13 Nov at 09:00", or "tomorrow at 09:00" / "today at 09:00" when `now` is given
    and the slot is that close (the urgent wording)."""
    local = start.astimezone(tz)
    clock = f"{local:%H:%M}"
    if now is not None:
        delta = (local.date() - now.astimezone(tz).date()).days
        if delta == 0:
            return f"today at {clock}"
        if delta == 1:
            return f"tomorrow at {clock}"
    return f"{fmt_day(local.date())} at {clock}"


def _on(slot: str) -> str:
    """ "on Thu 13 Nov at 09:00", but "tomorrow at 09:00" (the urgent wording) takes no "on"."""
    return slot if slot.startswith(("today", "tomorrow")) else f"on {slot}"


def service_word(service: str) -> str:
    return service.replace("_", " ")


def replacement_notice(
    provider: str, service: str, slot: str, booking_id: str, held_until: str | None
) -> str:
    held = f", held until {held_until}" if held_until else ""
    return (
        f"{provider} cancelled your {service_word(service)} visit {_on(slot)} ({booking_id}). "
        f"Here is a replacement that fits what you asked for{held}:"
    )


def nothing_fits_notice(
    provider: str, service: str, slot: str, booking_id: str, searched: str
) -> str:
    return (
        f"{provider} cancelled your {service_word(service)} visit {_on(slot)} ({booking_id}). "
        f"I looked at {searched} and nothing fits what you asked for, so I haven't held "
        "anything. You can widen the search with one tap:"
    )


def nothing_to_cancel(provider: str, service: str, slot: str, booking_id: str) -> str:
    return (
        f"{provider} cancelled on their side, so there is nothing for you to cancel: your "
        f"{service_word(service)} visit {_on(slot)} ({booking_id}) is off."
    )


def already_have(existing_provider: str, existing_slot: str, existing_id: str) -> str:
    return (
        f"You already have {existing_provider} on {existing_slot} ({existing_id}), "
        "so I haven't looked for another."
    )


def ask_where(provider: str, service: str, slot: str, booking_id: str) -> str:
    return (
        f"{provider} cancelled your {service_word(service)} visit {_on(slot)} ({booking_id}). "
        f"{ask_area()}"
    )


def dismissed_line(booking_id: str) -> str:
    return f"Okay, I've stopped looking for a replacement for {booking_id}. Ask me any time."


def stale_line() -> str:
    return "That follow-up has already been handled, so there is nothing more to do."


def widened_notice(booking_id: str, held_until: str | None) -> str:
    held = f", held until {held_until}" if held_until else ""
    return f"With the wider search, here is a replacement for {booking_id}{held}:"


def ask_area() -> str:
    return "Where should I look?"


def origin_line(provider: str, booking_id: str, slot: str) -> str:
    return f"{provider} cancelled {booking_id} ({slot})."


def origin_line_rebooked(new_booking_id: str, slot: str, old_booking_id: str) -> str:
    return f"Rebooked {new_booking_id} for {slot}, replacing {old_booking_id}."


def lapse_line(booking_id: str) -> str:
    return (
        f"The window for replacing {booking_id} has passed, so I've stopped looking. "
        "Ask me any time."
    )


def bell_title(service: str, *, replacement_ready: bool | None) -> str:
    """ "Plumber visit cancelled · replacement ready" or "· nothing fits yet"; no suffix when
    no search was made."""
    base = f"{service_word(service).capitalize()} visit cancelled"
    if replacement_ready is None:
        return base
    return base + (" · replacement ready" if replacement_ready else " · nothing fits yet")
