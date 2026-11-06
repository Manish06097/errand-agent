"""Trace record schema v1 (architecture 16.2).

Every record is the envelope plus the fields of its `type`, validated as a
discriminated union on `type` before it reaches any sink. An unknown type, or an
unknown field on a known type, is rejected at emit time. Fields that later
milestones fill are optional here, so they can start emitting without a schema
bump; anything new they need is added to this module.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, TypeAdapter

from errand.usage import Usage

SCHEMA_VERSION = 1

Json = Any


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    seq: NonNegativeInt
    ts: str  # simulated or system clock, ISO 8601 with offset and milliseconds
    wall_ts: str  # real time, same format
    run_id: str
    thread_id: str
    errand_id: str | None = None
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    node_attempt: int = Field(default=1, ge=1)


class RunTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls: NonNegativeInt
    tool_calls: NonNegativeInt
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    cost_usd: NonNegativeFloat
    latency_ms: NonNegativeFloat


class RunStart(Envelope):
    type: Literal["run.start"]
    trigger: str
    inbound_type: str | None = None
    user_message_id: str | None = None


class RunEnd(Envelope):
    type: Literal["run.end"]
    outcome: str
    user_message_id: str | None = None
    totals: RunTotals


class NodeRecord(Envelope):
    type: Literal["node.start", "node.end"]
    node: str
    phase_before: str | None = None
    phase_after: str | None = None
    error: str | None = None
    latency_ms: NonNegativeFloat | None = None


class StateTransition(Envelope):
    type: Literal["state.transition"]
    from_: str = Field(alias="from")
    to: str
    reason: str

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ContextBuilt(Envelope):
    type: Literal["context.built"]
    manifest: dict[str, Json]


class ThinkingSummary(BaseModel):
    """A thinking block as the sanitizer leaves it: summary text and signature digest."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature_sha256: str | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    tool_calls: list[dict[str, Json]]
    thinking: list[ThinkingSummary] = Field(default_factory=list)


class ModelCall(Envelope):
    type: Literal["model.call"]
    model: str
    mode: str
    effort: str | None
    thinking_display: str | None
    request_sha256: str
    request: dict[str, Json]  # sanitized canonical request
    response: ModelResponse
    usage: Usage
    cost_usd: NonNegativeFloat
    latency_ms: NonNegativeFloat
    # Time to the first chunk carrying answer text or tool-use input. Empty chunks
    # (Anthropic's message_start) and thinking deltas do not count, since the user
    # never sees thinking. None when the response had neither, e.g. thinking only.
    ttft_ms: NonNegativeFloat | None
    stop_reason: str | None
    input_transformations: list[Json] = Field(default_factory=list)
    est_request_tokens: NonNegativeInt
    floor_tokens: NonNegativeInt
    budget_tokens: NonNegativeInt


class ReaderCall(Envelope):
    type: Literal["reader.call"]
    provider_id: str
    reviews_sha256: str
    n_reviews: NonNegativeInt
    cache_hit: bool
    labels: dict[str, Json] | list[Json] | None = None
    usage: Usage | None = None
    cost_usd: NonNegativeFloat | None = None


class ToolCall(Envelope):
    type: Literal["tool.call"]
    tool: str
    caller: str
    args: dict[str, Json]
    attempt: int = 1
    idempotency_key: str | None = None
    result: Json = None
    error: str | None = None
    latency_ms: NonNegativeFloat | None = None
    world_seq: int | None = None


class BriefRecord(Envelope):
    type: Literal["brief.patch", "invalidate"]
    fields: list[str] | dict[str, Json] | None = None
    evidence_ok: bool | None = None
    what: Json = None
    reason: str | None = None


class ApprovalRecord(Envelope):
    type: Literal[
        "approval.requested",
        "approval.granted",
        "approval.declined",
        "approval.invalidated",
        "approval.consumed",
        "approval.expired",
        "approval.hold_rebound",
    ]
    approval_id: str
    fingerprint: str | None = None
    via: str | None = None
    reason: str | None = None
    old_fp: str | None = None
    new_fp: str | None = None
    intent_id: str | None = None
    hold_id: str | None = None


class IntentRecord(Envelope):
    type: Literal[
        "intent.written", "intent.in_flight", "intent.unknown", "intent.retried", "intent.resolved"
    ]
    intent_id: str
    attempt: int | None = None
    idempotency_key: str | None = None
    hold_id: str | None = None
    state: str | None = None
    same_key_retries: int | None = None
    booking_id: str | None = None
    book_calls: int | None = None


class StatusCheck(Envelope):
    type: Literal["status.check"]
    idempotency_key: str
    result: Json = None
    hold_live: bool | None = None
    decision: (
        Literal[
            "same_key_retry", "report_success", "report_not_applied", "wait_for_lapse", "escalate"
        ]
        | None
    ) = None


class BudgetRecord(Envelope):
    type: Literal["budget.tick", "budget.exhausted"]
    counters: dict[str, Json]
    limit: str | None = None


class MemoryRecord(Envelope):
    type: Literal[
        "memory.written", "memory.confirmed", "memory.corrected", "memory.forgotten", "memory.used"
    ]
    memory_id: str
    kind: str | None = None
    status: str | None = None
    value_hash: str | None = None


class FollowupRecord(Envelope):
    type: Literal["followup.detected", "followup.ignored", "followup.notified"]
    booking_id: str
    urgency: str | None = None


class GuardRecord(Envelope):
    type: Literal[
        "guard.claim_rewritten",
        "guard.ask_rejected",
        "guard.evidence_rejected",
        "guard.forgotten_value_masked",
        "guard.planner_tool_rejected",
    ]
    detail: dict[str, Json] = Field(default_factory=dict)


class ErrorRecord(Envelope):
    type: Literal["error"]
    where: str
    exception_type: str
    message: str


TraceRecord = Annotated[
    RunStart
    | RunEnd
    | NodeRecord
    | StateTransition
    | ContextBuilt
    | ModelCall
    | ReaderCall
    | ToolCall
    | BriefRecord
    | ApprovalRecord
    | IntentRecord
    | StatusCheck
    | BudgetRecord
    | MemoryRecord
    | FollowupRecord
    | GuardRecord
    | ErrorRecord,
    Field(discriminator="type"),
]

RECORD_ADAPTER: TypeAdapter[TraceRecord] = TypeAdapter(TraceRecord)


def _record_types() -> frozenset[str]:
    names: set[str] = set()
    for cls in Envelope.__subclasses__():
        annotation = cls.model_fields["type"].annotation
        names.update(getattr(annotation, "__args__", ()))
    return frozenset(names)


RECORD_TYPES: frozenset[str] = _record_types()

# Records that must survive a crash of the process right after they were written.
FSYNC_PREFIXES = ("approval.", "intent.")
FSYNC_TYPES = frozenset({"run.end"})


def needs_fsync(record_type: str) -> bool:
    return record_type in FSYNC_TYPES or record_type.startswith(FSYNC_PREFIXES)
