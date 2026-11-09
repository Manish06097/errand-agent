"""The planner's tools: their model-facing schemas and the models that parse their input.

The list is fixed for the whole loop and sent in this order with `strict=True` (10.3).
Its bytes are part of the prefix that thinking signatures are bound to, so it is
built once, as literals, and never filtered or re-ordered mid-loop: the ask guard and
the argument checks run in code instead.

Strict tool use needs every property listed in `required` and `additionalProperties:
false` on every object, so optional values are nullable rather than omitted.
`tests/agent/test_planner_tools.py` checks the schemas against those rules, because
scripted mode would accept a schema the API rejects.

`book`, `cancel`, `reschedule`, `hold_slot` and `get_booking_status` are absent by
construction (INV-11 checks every traced request), and the ToolGateway refuses them
for the planner anyway. `propose_booking` (M4) only names a slot handle; code holds the
slot, prices it, renders the card and waits for the user (agent/validation.py checks
the proposal first). `propose_cancel` and `propose_reschedule` join it in M9, after it
in the fixed order. The list's measured size is pinned by
`test_the_tool_list_fits_its_context_section`: 1,996 of the 2,000-token section in M4
(1,809 before propose_booking), so M9 and M11 need room made for theirs.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from errand.domain.brief import BriefField, PartOfDay
from errand.world.contracts import Area, Service


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_PATCHABLE_FIELDS = ["service", "issue", "area", "window", "budget_max_inr"]
_ASKABLE_FIELDS = [f.value for f in BriefField]

SEARCH_PROVIDERS: dict[str, Any] = {
    "name": "search_providers",
    "description": (
        "Search the brief's service, area and dates; no arguments. Fails, naming the gaps, "
        "if a required field is missing."
    ),
    "input_schema": _object({}),
}
GET_PROVIDER: dict[str, Any] = {
    "name": "get_provider",
    "description": ("One provider from the shortlist or the user's bookings."),
    "input_schema": _object({"provider_id": {"type": "string"}}),
}
LIST_BOOKINGS: dict[str, Any] = {
    "name": "list_bookings",
    "description": "The user's bookings. No arguments.",
    "input_schema": _object({}),
}
UPDATE_BRIEF: dict[str, Any] = {
    "name": "update_brief",
    "description": (
        "Record what the user said. Each value set or cleared needs an exact quote from "
        "the current message (4+ chars) as evidence. Never write dates: give the user's "
        "time phrase and part of day."
    ),
    "input_schema": _object(
        {
            "set": _object(
                {
                    "service": _nullable({"type": "string", "enum": [s.value for s in Service]}),
                    "issue": _nullable({"type": "string"}),
                    "area": _nullable({"type": "string", "enum": [a.value for a in Area]}),
                    "window": _nullable(
                        _object(
                            {
                                "phrase": {"type": "string"},
                                "part_of_day": _nullable(
                                    {
                                        "type": "string",
                                        "enum": ["morning", "afternoon", "evening", "any"],
                                    }
                                ),
                            }
                        )
                    ),
                    "budget_max_inr": {
                        "anyOf": [
                            {"type": "integer"},
                            {"type": "string", "enum": ["none"]},
                            {"type": "null"},
                        ],
                    },
                }
            ),
            "clear": {"type": "array", "items": {"type": "string", "enum": _PATCHABLE_FIELDS}},
            "evidence": {
                "type": "array",
                "items": _object(
                    {
                        "field": {"type": "string", "enum": _PATCHABLE_FIELDS},
                        "quote": {"type": "string"},
                    }
                ),
            },
        }
    ),
}
ASK_USER: dict[str, Any] = {
    "name": "ask_user",
    "description": (
        "Ask for every missing field in one question, never a known one. A field may be "
        "asked again once, with quick-reply options (at most 6, each 40 chars or fewer)."
    ),
    "input_schema": _object(
        {
            "fields": {"type": "array", "items": {"type": "string", "enum": _ASKABLE_FIELDS}},
            "question": {"type": "string"},
            "options": _nullable({"type": "array", "items": {"type": "string"}}),
        }
    ),
}

PROPOSE_BOOKING: dict[str, Any] = {
    "name": "propose_booking",
    "description": "Propose one shortlisted slot; the user approves it on a card.",
    "input_schema": _object({"provider_id": {"type": "string"}, "slot_ref": {"type": "string"}}),
}

# N1: cancel by a booking handle from this run's list_bookings view (B1, B2, ...). The
# model never writes a booking id. Bound after propose_booking (10.3) once M8's 3,800
# token S2 cap is in; until then the handler exists and the tool is not offered.
PROPOSE_CANCEL: dict[str, Any] = {
    "name": "propose_cancel",
    "description": "Propose cancelling one booking by its list_bookings handle; the user approves.",
    "input_schema": _object({"booking_ref": {"type": "string"}}),
}

PLANNER_TOOLS: tuple[dict[str, Any], ...] = (
    SEARCH_PROVIDERS,
    GET_PROVIDER,
    LIST_BOOKINGS,
    UPDATE_BRIEF,
    ASK_USER,
    PROPOSE_BOOKING,
)
PROPOSE_TOOLS: frozenset[str] = frozenset({"propose_booking", "propose_cancel"})
PLANNER_TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in PLANNER_TOOLS)


# ---------------------------------------------------------------- parsed input


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetProviderArgs(_Args):
    provider_id: str = Field(min_length=1, max_length=40)


class WindowArg(_Args):
    """M3's closed set: a phrase and a part of day. `not_before` and `not_after` join the
    schema with the full resolver in M8 (the domain `TimeWindowExpr` already has them)."""

    phrase: str = Field(min_length=1, max_length=80)
    part_of_day: PartOfDay | None = None


class BriefSet(_Args):
    service: Service | None = None
    issue: str | None = None
    area: Area | None = None
    window: WindowArg | None = None
    budget_max_inr: int | Literal["none"] | None = None


PatchableName = Literal["service", "issue", "area", "window", "budget_max_inr"]


class EvidenceArg(_Args):
    field: PatchableName
    quote: str


class UpdateBriefArgs(_Args):
    set: BriefSet = Field(default_factory=BriefSet)
    clear: list[PatchableName] = Field(default_factory=list)
    evidence: list[EvidenceArg] = Field(default_factory=list)


# Bounds on the one piece of model text a ClarifyCard carries. They live here, not in
# the schema: strict tool use rejects maxItems and maxLength, so code enforces them
# and the description states them.
ASK_QUESTION_MAX_CHARS = 400
ASK_OPTIONS_MAX = 6
ASK_OPTION_MAX_CHARS = 40


class ProposeBookingArgs(_Args):
    # Strict mode rejects maxLength and pattern, so the handle's shape is checked in
    # code (agent/validation.py), not here.
    provider_id: str = Field(min_length=1, max_length=40)
    slot_ref: str = Field(min_length=1, max_length=8)


class ProposeCancelArgs(_Args):
    # Strict mode rejects pattern and maxLength; the handle's shape is checked in code.
    booking_ref: str = Field(min_length=1, max_length=8)


class AskUserArgs(_Args):
    fields: list[BriefField]
    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_CHARS)
    options: list[Annotated[str, Field(max_length=ASK_OPTION_MAX_CHARS)]] | None = Field(
        default=None, max_length=ASK_OPTIONS_MAX
    )
