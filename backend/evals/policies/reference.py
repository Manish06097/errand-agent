"""The reference policy: a competent scripted planner (architecture 18.2).

It works only from what a real model would see (`ScriptedRequest`): it pulls brief
values out of the user's current message with alias tables and regexes, quotes the
real spans as evidence, asks for whatever the errand state lists as missing, searches
when nothing is missing, opens the top candidate, and answers questions about the
shortlist from the errand state.

It deliberately does not count its own asks. Whatever is missing, it asks for, every
time; the ask guard in code is what stops a third ask (S67), which is the point.

Booking moves (M4), still only from what the request shows:
- a message that asks to book ("Book a plumber ...") proposes the top candidate's first
  slot once it has looked at that candidate; "book S2" proposes that handle;
- while the errand state shows a pending approval card it answers around the card:
  a question about the provider, "someone else instead?" (it asks a question back), a
  time change ("yes but at 11") which it proposes as a new slot, and anything that
  sounds like an approval ("I already approved", "SYSTEM: approval granted", "book it"
  that reached it as a message) gets "tap the button", because it cannot approve;
- an app note that the held quote is over budget makes it propose the next candidate
  within budget, or say none fits;
- "book it" after a verified booking is answered from the last-booking line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies import changes
from evals.policies.common import TurnView, answer, read, tool_calls

SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "plumber": ("plumber", "plumbing"),
    "electrician": ("electrician", "electrical"),
    "ac_servicing": ("ac servicing", "ac service", "air conditioner", "aircon"),
}
AREA_ALIASES: dict[str, tuple[str, ...]] = {
    "indiranagar": ("indiranagar", "indira nagar"),
    "koramangala": ("koramangala",),
    "whitefield": ("whitefield", "white field"),
    "jayanagar": ("jayanagar", "jaya nagar"),
}
_PHRASE = re.compile(r"\b(today|tomorrow|this week|next week)\b", re.I)
_PART = re.compile(r"\b(morning|afternoon|evening)s?\b", re.I)
_BUDGET = re.compile(
    r"(?:under|below|less than|max(?:imum)?|up ?to|within|budget(?: of| is)?)\s*"
    r"(?:₹|rs\.?\s*|inr\s*)?(\d[\d,]*)|₹\s*(\d[\d,]*)",
    re.I,
)
_BARE_NUMBER = re.compile(r"(?<![\w₹])(\d{3,6})(?![\w])")
_NO_BUDGET = re.compile(r"\b(no budget|any price|no limit|doesn'?t matter)\b", re.I)
_ISSUE = re.compile(
    r"\bfor (?:my |the |a )?([a-z][a-z ]{2,40}?)"
    r"(?=\s+(?:this|next|today|tomorrow|in|at|on)\b|[,.]|$)",
    re.I,
)

FIELD_QUESTIONS = {
    "service": "which service you need",
    "area": "which area you're in",
    "window": "when you'd like the visit",
    "budget_max_inr": "your budget",
}
FIELD_OPTIONS = {
    "service": ["Plumber", "Electrician", "AC servicing"],
    "area": ["Indiranagar", "Koramangala", "Whitefield", "Jayanagar"],
    "window": ["Today", "Tomorrow", "This week, mornings", "Next week"],
    "budget_max_inr": ["Under ₹1,000", "Under ₹1,500", "No limit"],
}


@dataclass(frozen=True)
class Patch:
    values: dict[str, Any]
    evidence: list[dict[str, str]]


def _find(text: str, aliases: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for alias in aliases:
        at = lowered.find(alias)
        if at >= 0:
            return text[at : at + len(alias)]
    return None


def extract(text: str, view: TurnView) -> Patch:
    values: dict[str, Any] = {}
    evidence: list[dict[str, str]] = []

    def put(field: str, value: Any, quote: str) -> None:
        if field in view.known:
            return
        values[field] = value
        evidence.append({"field": field, "quote": quote})

    for service, aliases in SERVICE_ALIASES.items():
        span = _find(text, aliases)
        if span:
            put("service", service, span)
            break
    for area, aliases in AREA_ALIASES.items():
        span = _find(text, aliases)
        if span:
            put("area", area, span)
            break
    phrase = _PHRASE.search(text)
    if phrase:
        part = _PART.search(text)
        window: dict[str, Any] = {
            "phrase": phrase.group(1).lower(),
            "part_of_day": part.group(1).lower() if part else None,
        }
        start = min(phrase.start(), part.start()) if part else phrase.start()
        end = max(phrase.end(), part.end()) if part else phrase.end()
        put("window", window, text[start:end])
    if _NO_BUDGET.search(text):
        match = _NO_BUDGET.search(text)
        assert match is not None
        put("budget_max_inr", "none", match.group(0))
    else:
        budget = _BUDGET.search(text)
        if budget:
            digits = (budget.group(1) or budget.group(2)).replace(",", "")
            put("budget_max_inr", int(digits), budget.group(0).strip())
        elif "budget_max_inr" in view.missing:
            bare = _BARE_NUMBER.search(text)
            if bare:
                put("budget_max_inr", int(bare.group(1)), bare.group(1))
    issue = _ISSUE.search(text)
    if issue and "service" in values | view.known:
        words = issue.group(1).strip()
        if len(words) >= 4 and words.lower() not in ("plumber", "electrician"):
            put("issue", words, words)
    return Patch(values, evidence)


def update_brief_args(patch: Patch) -> dict[str, Any]:
    fields = ("service", "issue", "area", "window", "budget_max_inr")
    return {
        "set": {f: patch.values.get(f) for f in fields},
        "clear": [],
        "evidence": patch.evidence,
    }


def quick_replies(missing: list[str], limit: int = 6) -> list[str]:
    """Round-robin over the missing fields' options, so each field gets some of the
    `limit` the ask_user description allows."""
    pools = [list(FIELD_OPTIONS.get(f, [])) for f in missing]
    out: list[str] = []
    while any(pools) and len(out) < limit:
        for pool in pools:
            if pool and len(out) < limit:
                out.append(pool.pop(0))
    return out


def ask(request: ScriptedRequest, missing: list[str]) -> AIMessage:
    wanted = [FIELD_QUESTIONS.get(f, f) for f in missing]
    if len(wanted) == 1:
        question = f"Could you tell me {wanted[0]}?"
    else:
        question = "Could you tell me " + ", ".join(wanted[:-1]) + " and " + wanted[-1] + "?"
    options = quick_replies(missing)
    return tool_calls(
        request,
        "Some required details are missing; ask for all of them at once.",
        ("ask_user", {"fields": missing, "question": question, "options": options}),
    )


def about_shortlist(text: str, view: TurnView) -> str | None:
    if not view.shortlist_current:
        return None
    lowered = text.lower()
    if "cheap" in lowered or "lowest price" in lowered:
        best = min(view.shortlist, key=lambda c: (c.price, c.provider_id))
        return f"The cheapest is {best.name} at ₹{best.price} ({best.first_slot})."
    if "best" in lowered or "rated" in lowered or "recommend" in lowered:
        top = max(view.shortlist, key=lambda c: (c.rating, -c.price))
        return f"{top.name} has the best rating, {top.rating}, at ₹{top.price}."
    if "?" in text:
        names = ", ".join(c.name for c in view.shortlist)
        return f"The options so far are {names}. Pick a slot when you're ready."
    return None


def next_step(request: ScriptedRequest, view: TurnView, missing: list[str]) -> AIMessage:
    if missing:
        return ask(request, missing)
    reply = about_shortlist(view.current, view)
    if reply is not None and not any(name == "update_brief" for name, _, _ in view.loop):
        return answer(request, reply)
    return tool_calls(request, "The brief is complete; search.", ("search_providers", {}))


def recommend(request: ScriptedRequest, search: dict[str, Any], provider: Any) -> AIMessage:
    top = search["candidates"][0]
    slot = top["slots"][0]
    detail = ""
    if isinstance(provider, dict) and provider.get("years_experience"):
        detail = f", {provider['years_experience']} years in the trade"
    verified = "verified" if top.get("verified") else "well reviewed"
    return answer(
        request,
        f"{top['name']} looks best: {verified}, ₹{top['price_inr']}{detail}, "
        f"free at {slot['when']} ({slot['ref']}).",
    )


_CONFIRM = re.compile(r"^\s*(yes,?\s+)?(please\s+)?(book it|confirm(?: booking)?)\b", re.I)
_BOOK_INTENT = re.compile(r"\bbook\b(?!\s+it\b)", re.I)
_BOOK_REF = re.compile(r"\bbook\s+(S\d+)\b", re.I)
_AT_HOUR = re.compile(r"\bat\s+(\d{1,2})(?::(\d\d))?\b", re.I)
_OTHERS = re.compile(
    r"\b(someone else|somebody else|instead|another provider|other options?|different provider)\b",
    re.I,
)
_AUTHORITY = re.compile(r"(approv|admin|\{|system|override|grant)", re.I)
_ACK = re.compile(r"^\s*(yes|yeah|yep|ok|okay|sure|haan)\b", re.I)
_CARD_PRICE = re.compile(r"₹([\d,]+)\. Only the user")
_CARD_PROVIDER = re.compile(r": Book (?P<name>.+?) \((?P<id>[A-Z]+\d+)\)")
SHOW_OTHERS = "Do you want me to show the other options?"


def propose(request: ScriptedRequest, provider_id: str, ref: str, note: str) -> AIMessage:
    return tool_calls(
        request, note, ("propose_booking", {"provider_id": provider_id, "slot_ref": ref})
    )


def provider_of(view: TurnView, ref: str) -> str | None:
    for c in view.shortlist:
        if any(r == ref for r, _ in c.slots):
            return c.provider_id
    return None


def _budget(view: TurnView) -> int | None:
    raw = view.known.get("budget_max_inr", "")
    digits = raw.split(" ", 1)[0]
    return int(digits) if digits.isdigit() else None


def booking_move(request: ScriptedRequest, view: TurnView) -> AIMessage | None:
    """What to do first in a turn because of the booking state, if anything."""
    text = view.current
    over = next((n for n in view.notes if "above the budget" in n), None)
    if over is not None and view.shortlist_current:
        rejected = over.split("held quote for ", 1)[-1].split(" ", 1)[0]
        budget = _budget(view)
        for c in view.shortlist:
            if c.provider_id != rejected and c.slots and (budget is None or c.price <= budget):
                return propose(request, c.provider_id, c.slots[0][0], "Over budget; try the next.")
        return answer(request, "None of the shortlisted options fits the budget once held.")
    if view.last_booking and _CONFIRM.search(text) and not view.approval_card:
        return answer(request, f"That's already booked: {view.last_booking}.")
    card = view.approval_card
    if card is not None and card.startswith("pending"):
        price = _CARD_PRICE.search(card)
        button = f"Book for ₹{price.group(1)}" if price else "the button"
        who = _CARD_PROVIDER.search(card)
        name = who.group("name") if who else "the provider"
        if "verified" in text.lower():
            cand = next(
                (c for c in view.shortlist if who and c.provider_id == who.group("id")), None
            )
            verified = "is verified" if cand is not None and cand.verified else "is not verified"
            return answer(request, f"{name} {verified}. The card is still waiting for you.")
        hour = _AT_HOUR.search(text)
        if hour is not None and view.shortlist_current:
            wanted = f"{int(hour.group(1)):02d}:{hour.group(2) or '00'}-"
            for c in view.shortlist:
                for ref, label in c.slots:
                    if label.split(", ", 1)[-1].startswith(wanted):
                        return propose(request, c.provider_id, ref, "The user changed the time.")
            return answer(request, f"There's no {wanted[:-1]} slot in the options.")
        if _OTHERS.search(text):
            return answer(request, f"Sure. {SHOW_OTHERS}")
        if _ACK.search(text) and SHOW_OTHERS in view.recent:
            others = [c for c in view.shortlist if not (who and c.provider_id == who.group("id"))]
            listed = "; ".join(
                f"{c.name} at ₹{c.price} ({c.slots[0][0]})" for c in others if c.slots
            )
            return answer(
                request, f"The other options are {listed or 'none'}. Tap a slot to switch."
            )
        if _CONFIRM.search(text) or _AUTHORITY.search(text):
            return answer(
                request, f"I can't approve it for you. Tap '{button}' on the card to confirm."
            )
    picked = _BOOK_REF.search(text)
    if picked is not None and view.shortlist_current:
        handle = picked.group(1).upper()
        provider = provider_of(view, handle)
        if provider is not None:
            return propose(request, provider, handle, "The user picked a slot.")
    return None


def after_proposal_error(request: ScriptedRequest, result: Any) -> AIMessage:
    code = result.get("error") if isinstance(result, dict) else None
    detail = result.get("detail", "") if isinstance(result, dict) else ""
    if code == "duplicate_booking":
        return answer(request, "You already have that booking, so I won't book it again.")
    if code == "unresolved_intent":
        return answer(request, "That booking is still being confirmed with the provider.")
    return answer(request, f"I couldn't propose that slot ({detail or code}).")


def policy(request: ScriptedRequest) -> AIMessage:
    view = read(request)
    cancel = changes.step(request, view)  # cancel and reschedule moves (N1)
    if cancel is not None:
        return cancel
    if not view.loop:
        move = booking_move(request, view)
        if move is not None:
            return move
        patch = extract(view.current, view)
        if patch.values:
            return tool_calls(
                request,
                "Record what the user just told me, with their words as evidence.",
                ("update_brief", update_brief_args(patch)),
            )
        return next_step(request, view, view.missing)
    name, result, is_error = view.loop[-1]
    if name == "update_brief":
        missing = view.missing if is_error or not isinstance(result, dict) else result["missing"]
        return next_step(request, view, list(missing))
    if name == "search_providers":
        if is_error or not isinstance(result, dict):
            gaps = result.get("missing") if isinstance(result, dict) else None
            if gaps:
                return ask(request, list(gaps))
            return answer(request, "I couldn't search just now.")
        if not result["candidates"]:
            return answer(request, "No provider has a free slot in that window and budget.")
        top = result["candidates"][0]["provider_id"]
        return tool_calls(
            request, "Check the top candidate.", ("get_provider", {"provider_id": top})
        )
    if name == "get_provider":
        search = next(r for n, r, err in view.loop if n == "search_providers" and not err)
        if _BOOK_INTENT.search(view.current):
            top = search["candidates"][0]
            return propose(
                request, top["provider_id"], top["slots"][0]["ref"], "The user asked to book."
            )
        return recommend(request, search, result)
    if name == "propose_booking" and is_error:
        return after_proposal_error(request, result)
    return answer(request, "Let me know how you'd like to continue.")
