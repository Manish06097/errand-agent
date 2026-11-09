"""Cancel and reschedule behaviour of the reference policy (N1), kept apart from the
booking moves so the booking policy's extraction does not change.

Still only from what the request shows. A message that asks to cancel makes it list the
user's bookings; from that listing it picks the one booking whose service the message
names (the handle `B1`, `B2`, ... the view gave, never an id) and proposes cancelling it.
When more than one matches it asks which one, listing them, in plain text (REPLIED): there
is no choice card for it. While a cancel card is pending it answers around the card, the
way the booking moves do: "keep it instead?" gets a question back (so the reminder
refuses typed grants), a bare "yes" to that question gets a statement, and anything that
sounds like an approval gets "tap the button".
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies.common import TurnView, answer, tool_calls

_CANCEL = re.compile(r"\bcancel\b", re.I)
_CANCEL_ID = re.compile(r"\bcancel\s+(B-\d+)\b", re.I)
_CANCEL_IT = re.compile(r"^\s*(yes,?\s+)?(please\s+)?(cancel it|confirm cancellation)\b", re.I)
_ACK = re.compile(r"^\s*(yes|yeah|yep|ok|okay|sure|haan)\b", re.I)
_KEEP = re.compile(r"\b(keep|instead|change my mind|not sure|wait)\b", re.I)
_AUTHORITY = re.compile(r"(approv|admin|\{|system|override|grant)", re.I)
KEEP_QUESTION = "Do you want me to leave the booking as it is?"
SERVICE_WORDS = {
    "ac_servicing": re.compile(r"\b(ac|a/c|air ?con\w*|air conditioner)\b", re.I),
    "plumber": re.compile(r"\b(plumber|plumbing)\b", re.I),
    "electrician": re.compile(r"\b(electrician|electrical)\b", re.I),
}


def _asked_services(text: str) -> set[str]:
    return {name for name, rx in SERVICE_WORDS.items() if rx.search(text)}


def _matches(text: str, bookings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    named = _CANCEL_ID.search(text)
    if named is not None:
        # "Cancel B-8266": the user quotes an id (a button the notice offered); the handle
        # is still the view's.
        return [b for b in bookings if b.get("booking_id") == named.group(1)]
    wanted = _asked_services(text)
    active = [b for b in bookings if b.get("status") == "confirmed"]
    return [b for b in active if not wanted or b.get("service") in wanted]


def step(request: ScriptedRequest, view: TurnView) -> AIMessage | None:
    """The reference policy's cancel moves; None when this turn is not about a cancel."""
    text = view.current
    card = view.approval_card
    if not view.loop:
        if card is not None and card.startswith("pending") and " Cancel " in f" {card}":
            if _CANCEL_IT.search(text) or _AUTHORITY.search(text):
                return answer(
                    request, "I can't approve it for you. Tap 'Cancel booking' on the card."
                )
            if _KEEP.search(text):
                return answer(request, f"Sure. {KEEP_QUESTION}")
            if _ACK.search(text) and KEEP_QUESTION in view.recent:
                return answer(
                    request,
                    "Okay, the card stays as it is. Tap 'Cancel booking' only if you want it "
                    "cancelled.",
                )
            return None
        if _CANCEL.search(text) and not (card is not None and card.startswith("pending")):
            return tool_calls(
                request, "The user wants to cancel; look at their bookings.", ("list_bookings", {})
            )
        return None
    name, result, is_error = view.loop[-1]
    if name == "list_bookings" and _CANCEL.search(text) and not is_error:
        bookings = list((result or {}).get("bookings") or [])
        found = _matches(text, bookings)
        if len(found) == 1:
            return tool_calls(
                request,
                "One booking matches; propose cancelling it.",
                ("propose_cancel", {"booking_ref": found[0]["ref"]}),
            )
        if not found:
            return answer(request, "I don't see an active booking like that to cancel.")
        listed = "; ".join(f"{b['provider_name']}, {b['when']} ({b['booking_id']})" for b in found)
        return answer(request, f"Which one do you mean? {listed}")
    if name == "propose_cancel" and is_error:
        detail = (result or {}).get("detail", "") if isinstance(result, dict) else ""
        return answer(request, f"I couldn't prepare that cancellation ({detail}).")
    return None
