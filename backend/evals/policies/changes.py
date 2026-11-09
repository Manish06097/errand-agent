"""Cancel, reschedule and history behaviour of the reference policy (N1), kept apart from
the booking moves so the booking policy's extraction does not change.

Still only from what the request shows. A message that asks to cancel makes it list the
user's bookings; from that listing it picks the one booking whose service the message
names (the handle `B1`, `B2`, ... the view gave, never an id) and proposes cancelling it.
When more than one matches it asks which one, listing them, in plain text (REPLIED): there
is no choice card for it. While a cancel card is pending it answers around the card, the
way the booking moves do: "keep it instead?" gets a question back (so the reminder
refuses typed grants), a bare "yes" to that question gets a statement, and anything that
sounds like an approval gets "tap the button".

A message that asks to move a booking ("Move my AC servicing to Saturday") lists the
bookings, records the day with update_brief (the evidence is the user's own words), and
proposes the move by handle. "Book the same electrician as last time" lists the bookings,
pins the matching one with update_brief (`provider_pref` is its handle, the evidence the
user's "same ... as last time"), asks whatever is still missing, and once nothing is, opens
the pinned provider and proposes its first slot that fits.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies.common import TurnView, answer, tool_calls

_CANCEL = re.compile(r"\bcancel\b", re.I)
_CANCEL_ID = re.compile(r"\bcancel\s+(B-\d+)\b", re.I)
_MOVE = re.compile(r"\b(move|reschedule|shift)\b", re.I)
_HISTORY = re.compile(r"\bsame\b[^.?!]{0,40}\bas last time\b|\blast time\b", re.I)
_BOOK = re.compile(r"\bbook\b", re.I)
_PINNED = re.compile(r"\((\w+)\)")
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
    history = history_step(request, view)
    if history is not None:
        return history
    moved = move_step(request, view)
    if moved is not None:
        return moved
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


def _listed(view: TurnView) -> list[dict[str, Any]]:
    result = next((r for n, r, err in view.loop if n == "list_bookings" and not err), None)
    return list((result or {}).get("bookings") or [])


def move_step(request: ScriptedRequest, view: TurnView) -> AIMessage | None:
    """Move a booking to a new day: list, record the day, propose by handle."""
    from evals.policies import reference

    text = view.current
    card = view.approval_card
    if card is not None and card.startswith("pending") and " Move " in f" {card}":
        approving = _AUTHORITY.search(text) or re.search(r"^\s*(yes,?\s+)?move it\b", text, re.I)
        if not view.loop and approving:
            return answer(request, "I can't approve it for you. Tap 'Move booking' on the card.")
        return None
    if not _MOVE.search(text) or _BOOK.search(text):
        return None
    if not view.loop:
        return tool_calls(
            request, "The user wants to move a booking; look at it.", ("list_bookings", {})
        )
    name, result, is_error = view.loop[-1]
    if name == "list_bookings" and not is_error:
        found = _matches(text, _listed(view))
        if len(found) != 1:
            if not found:
                return answer(request, "I don't see an active booking like that to move.")
            listed = "; ".join(f"{b['provider_name']}, {b['when']}" for b in found)
            return answer(request, f"Which one do you mean? {listed}")
        window = reference.window_value(text)
        if window is None:
            return answer(request, "Which day would you like it moved to?")
        patch = reference.Patch({"window": window[0]}, [{"field": "window", "quote": window[1]}])
        return tool_calls(
            request,
            "Record the new day with the user's words as evidence.",
            ("update_brief", reference.update_brief_args(patch)),
        )
    if name == "update_brief" and not is_error:
        found = _matches(text, _listed(view))
        if len(found) == 1:
            return tool_calls(
                request,
                "The new day is set; propose the move.",
                ("propose_reschedule", {"booking_ref": found[0]["ref"]}),
            )
    if name == "update_brief" and is_error:
        return answer(request, "I couldn't read that day. Which day would you like?")
    if name == "propose_reschedule" and is_error:
        detail = (result or {}).get("detail", "") if isinstance(result, dict) else ""
        return answer(request, f"I couldn't prepare that move ({detail}).")
    return None


def history_step(request: ScriptedRequest, view: TurnView) -> AIMessage | None:
    """ "Book the same electrician as last time": pin it from the bookings, then book."""
    from evals.policies import reference

    text = view.current
    pinned = _PINNED.search(view.known.get("provider_pref", ""))
    if view.loop and pinned is not None and not _HISTORY.search(text):
        name, result, is_error = view.loop[-1]
        if (
            name == "update_brief"
            and not is_error
            and isinstance(result, dict)
            and not result.get("missing")
        ):
            return tool_calls(
                request,
                "The user chose this provider by history; open it.",
                ("get_provider", {"provider_id": pinned.group(1)}),
            )
        if name == "get_provider" and not is_error and isinstance(result, dict):
            slot = next((s for s in result.get("available_slots", []) if s.get("ref")), None)
            if slot is not None:
                return tool_calls(
                    request,
                    "The first slot that fits.",
                    (
                        "propose_booking",
                        {"provider_id": result["provider_id"], "slot_ref": slot["ref"]},
                    ),
                )
            return answer(request, "That provider has no free time that fits.")
        return None
    if not (_HISTORY.search(text) and _BOOK.search(text)):
        return None
    if not view.loop:
        return tool_calls(
            request, "The user means a provider from their history.", ("list_bookings", {})
        )
    name, result, is_error = view.loop[-1]
    if name == "list_bookings" and not is_error:
        found = [b for b in _listed(view) if b.get("service") in _asked_services(text)]
        if not found:
            return answer(request, "I don't see an earlier booking like that.")
        quote = re.search(r"same [a-z ]+? as last time|last time", text, re.I)
        assert quote is not None
        patch_args = reference.update_brief_args(reference.Patch({}, []))
        patch_args["set"]["provider_pref"] = found[-1]["ref"]
        patch_args["evidence"] = [{"field": "provider_pref", "quote": quote.group(0)}]
        return tool_calls(
            request, "Pin the provider from the history.", ("update_brief", patch_args)
        )
    if name == "update_brief" and not is_error and isinstance(result, dict):
        gaps = list(result.get("missing") or [])
        if gaps:
            return reference.ask(request, gaps)
    return None
