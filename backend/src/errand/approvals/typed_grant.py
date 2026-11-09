"""Whether a typed message grants the live card (architecture 8.3). Pure.

The messages route gathers the inputs under the thread lock, after the sweep, and
calls `decide`; a GRANT then goes through the same conditional UPDATE as the button.
Every condition from 8.3, in order; the first that fails makes the message ordinary:

1. there is one live pending approval;
2. the text is an action phrase for the card's action (the closed grammar, 32
   characters at most, no digit, time, weekday, negation or "but");
3. bound to the card the user saw: `visible_card` was sent (captured when the user
   pressed Send) and names the live approval and its fingerprint;
4. bound to the conversation position: its `message_id` is the latest assistant
   message, that message is the card or its reminder, and it has `typed_grant_ok`;
5. bound to time: the client acked the card's render before `client_composed_at`, the
   approval was requested before `client_composed_at`, and `client_composed_at` is not
   later than the agent clock at receipt plus a small tolerance (a client clock that
   runs ahead could otherwise stamp a message typed before the render with a time after
   it). The render ack is below `client_composed_at`, so it is bounded too.

A bare acknowledgement never grants. It gets the code hint only when the latest
assistant message is the live card with `typed_grant_ok`; otherwise it is probably
answering a question and goes to the planner like any message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from errand.approvals.confirm_grammar import Action, Typed, classify
from errand.store.repos.approvals import ApprovalRow
from errand.store.repos.messages import Message


def is_rebook(row: ApprovalRow) -> bool:
    """Whether the card's material names a booking it replaces (a Rebook card)."""
    try:
        material = json.loads(row.material_json)
    except ValueError:
        return False
    return isinstance(material, dict) and material.get("replaces_booking_id") is not None


@dataclass(frozen=True)
class VisibleCard:
    approval_id: str
    fingerprint: str
    message_id: str


class Decision(StrEnum):
    GRANT = "grant"
    HINT = "hint"
    ORDINARY = "ordinary"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str


def decide(
    *,
    text: str,
    live: ApprovalRow | None,
    visible_card: VisibleCard | None,
    composed_at_ms: int | None,
    latest: Message | None,
    now_ms: int,
    tolerance_ms: int,
) -> Verdict:
    if live is None or live.status != "pending":
        return Verdict(Decision.ORDINARY, "no_live_card")
    action = live.action
    if action not in ("book", "cancel", "reschedule"):
        return Verdict(Decision.ORDINARY, "unknown_action")
    kind = classify(text, cast(Action, action), rebook=is_rebook(live))
    latest_is_card = (
        latest is not None
        and latest.role == "assistant"
        and latest.approval_id == live.approval_id
        and latest.typed_grant_ok is True
    )
    if kind is Typed.ACK:
        return Verdict(Decision.HINT if latest_is_card else Decision.ORDINARY, "bare_ack")
    if kind is Typed.OTHER:
        return Verdict(Decision.ORDINARY, "not_an_action_phrase")
    if visible_card is None:
        return Verdict(Decision.ORDINARY, "no_visible_card")
    if visible_card.approval_id != live.approval_id:
        return Verdict(Decision.ORDINARY, "visible_card_is_not_the_live_card")
    if visible_card.fingerprint != live.fingerprint:
        return Verdict(Decision.ORDINARY, "fingerprint_mismatch")
    if latest is None or visible_card.message_id != latest.message_id:
        return Verdict(Decision.ORDINARY, "not_the_latest_message")
    if not latest_is_card:
        return Verdict(Decision.ORDINARY, "latest_message_does_not_allow_typed_grants")
    if composed_at_ms is None:
        return Verdict(Decision.ORDINARY, "no_compose_time")
    if composed_at_ms > now_ms + tolerance_ms:
        return Verdict(Decision.ORDINARY, "composed_in_the_future")
    if latest.rendered_at is None or not latest.rendered_at < composed_at_ms:
        return Verdict(Decision.ORDINARY, "composed_before_render")
    if not live.requested_at < composed_at_ms:
        return Verdict(Decision.ORDINARY, "composed_before_request")
    return Verdict(Decision.GRANT, "bound")
