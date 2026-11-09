"""What starts or resumes a run. Every input, from any source, goes through `ingress`.

M4 sends `UserMessage`, `UiAction` (`select_slot`, `refresh_hold`), `ApprovalGranted`
and `ApprovalDeclined`; the other members exist so the checkpoint shape and the routing
function do not change when M5 to M14 start sending them.

An inbound is a hint, never an authority: `execute` re-reads the approval row, so a
forged `ApprovalGranted` carries no permission (8.3).
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
    # Set by the messages route when the text is a bare acknowledgement ("ok", "yes")
    # while the latest card allows typed grants: the run answers with the code hint and
    # never calls the model. It never grants anything.
    confirm_hint: bool = False


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
        "widen_followup",
        "dismiss_followup",
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
    kind: Literal["followup_start", "followup_lapsed"]
    booking_id: str | None = None
    job_id: str | None = None
    # followup_start only: delivered outside the saved contact window, so the badge only
    # (decision 7); set by delivery, never by the user.
    quiet: bool = False


class Recover(_Inbound):
    type: Literal["recover"] = "recover"
    intent_id: str
    # True when the reconciler found the intent at startup (the user is told "I checked
    # after a restart"); False for its periodic re-check in a running process.
    after_restart: bool = True


Inbound = Annotated[
    UserMessage | UiAction | ApprovalGranted | ApprovalDeclined | SystemEvent | Recover,
    Field(discriminator="type"),
]
