"""What starts or resumes a run. Every input, from any source, goes through `ingress`.

M3 produces only `UserMessage`; the other members exist so the checkpoint shape and
the routing function do not change when M4 to M14 start sending them.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Inbound(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserMessage(_Inbound):
    type: Literal["user_message"] = "user_message"
    message_id: str
    client_msg_id: str | None = None
    text: str
    turn_no: int


class UiAction(_Inbound):
    type: Literal["ui_action"] = "ui_action"
    client_action_id: str
    kind: Literal[
        "select_slot",
        "refresh_hold",
        "edit_brief_field",
        "choose_option",
        "check_now",
        "abandon_errand",
        "stop",
    ]
    payload: dict[str, str] = Field(default_factory=dict)


class ApprovalGranted(_Inbound):
    type: Literal["approval_granted"] = "approval_granted"
    approval_id: str
    via: Literal["button", "typed"]
    message_id: str | None = None


class ApprovalDeclined(_Inbound):
    type: Literal["approval_declined"] = "approval_declined"
    approval_id: str


class SystemEvent(_Inbound):
    type: Literal["system_event"] = "system_event"
    kind: Literal["followup_start"]
    booking_id: str | None = None


class Recover(_Inbound):
    type: Literal["recover"] = "recover"
    intent_id: str


Inbound = Annotated[
    UserMessage | UiAction | ApprovalGranted | ApprovalDeclined | SystemEvent | Recover,
    Field(discriminator="type"),
]
