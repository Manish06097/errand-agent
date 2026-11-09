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
    # Quarantined reader calls that reached the model; their cost and tokens are in the
    # totals above, but they are not model_calls (M10, additive).
    reader_calls: NonNegativeInt = 0


class RunStart(Envelope):
    type: Literal["run.start"]
    trigger: str
    inbound_type: str | None = None
    user_message_id: str | None = None
    # What the client sent with the message, recorded as received (M4): when it says
    # the message was composed, and the card it says the user was looking at.
    client_composed_at: str | None = None
    visible_card: dict[str, Json] | None = None
    routed_as: str | None = None
    # The limits this run was held to (N3, additive), so INV-09 reads them from the record
    # and never imports config.
    limits: dict[str, Json] | None = None
    # A watcher tick that saw a change or a failure: its own trace, no runs row (INV-07).
    kind: Literal["watch"] | None = None


class RunEnd(Envelope):
    type: Literal["run.end"]
    # None only for a watch run, which has no outcome.
    outcome: str | None = None
    user_message_id: str | None = None
    totals: RunTotals
    # The approval the run ended parked on, read from the graph's v2 output interrupts,
    # not from agent state (M4). None when the run did not end at an interrupt.
    parked_approval_id: str | None = None
    # The cap that ended the run, when one did (N3, additive): model_calls, iterations,
    # tool_calls or deadline.
    limit: str | None = None
    # A watch run (no outcome): what the tick saw.
    changes: list[dict[str, Json]] | None = None
    failed_users: list[str] | None = None
    # Set by the reconciler when it ends a run the process died in (N1): the recovery
    # run that took the intent over, or None when no recovery run was needed.
    recovered_by: str | None = None


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
    # Set on a call that failed (N3): usage and cost are zero, the response is empty.
    error: str | None = None


class ReaderCall(Envelope):
    """One quarantined reader read. Never a request body and never review text: ids,
    hashes, enum labels, usage and how it ended."""

    type: Literal["reader.call"]
    provider_id: str
    reviews_sha256: str
    n_reviews: NonNegativeInt
    cache_hit: bool
    labels: dict[str, Json] | list[Json] | None = None
    usage: Usage | None = None
    cost_usd: NonNegativeFloat | None = None
    # Added in M10 (schema v1, additive); M15 maps these to a Langfuse generation.
    reader_model: str | None = None
    latency_ms: NonNegativeFloat | None = None
    stop_reason: str | None = None
    # Why there is no digest: timeout, refusal, max_tokens, schema_violation,
    # transport_error, cap_reached, deadline, cost_cap, incomplete_labels, not_retried:<why>.
    # None when labels were produced.
    error: str | None = None
    request_sha256: str | None = None
    # Counts of what code dropped from an otherwise valid answer (unknown ids,
    # duplicates, themes past 3). Counts only, so no model-chosen string lands here.
    dropped: dict[str, NonNegativeInt] | None = None
    # The model id, for the Langfuse generation (M15). Added in the M10 review pass.
    model: str | None = None
    # A cache or refusal-marker store error that was absorbed (lookup, put,
    # refusal_lookup, mark); the read itself is unaffected.
    cache_error: str | None = None


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
        "approval.consume_refused",
    ]
    approval_id: str
    fingerprint: str | None = None
    via: str | None = None
    reason: str | None = None
    # approval.consume_refused: the row's status when the consume was refused.
    status: str | None = None
    old_fp: str | None = None
    new_fp: str | None = None
    intent_id: str | None = None
    hold_id: str | None = None
    # approval.hold_rebound: the hold the approval was bound to before the re-hold.
    old_hold_id: str | None = None
    # approval.requested: when the card lapses (agent clock), and what it replaced.
    expires_at: str | None = None
    superseded: list[str] | None = None
    message_id: str | None = None


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
    error: str | None = None


class StatusCheck(Envelope):
    type: Literal["status.check"]
    idempotency_key: str
    result: Json = None
    hold_live: bool | None = None
    decision: (
        Literal[
            "same_key_retry",
            "report_success",
            "report_provider_cancelled",
            "report_not_applied",
            "wait_for_lapse",
            "send_prepared",
            "escalate",
        ]
        | None
    ) = None
    # Set when the confirmed booking could not be checked against the approved price,
    # because the book answer that carried the price never arrived (a timeout).
    price_unverified: bool | None = None
    mismatch: list[str] | None = None
    # The comparison could not be finished (list_bookings unavailable). Never a mismatch.
    check_incomplete: bool | None = None


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
    type: Literal["followup.detected", "followup.ignored", "followup.notified", "followup.done"]
    booking_id: str
    urgency: str | None = None
    job_id: str | None = None
    # ignored: slot_started or already_replaced; done: replaced, dismissed or lapsed.
    reason: str | None = None
    variant: str | None = None


class GuardRecord(Envelope):
    type: Literal[
        "guard.claim_rewritten",
        "guard.ask_rejected",
        "guard.evidence_rejected",
        "guard.forgotten_value_masked",
        "guard.planner_tool_rejected",
        "guard.reprompt",
    ]
    detail: dict[str, Json] = Field(default_factory=dict)


class CardRecord(Envelope):
    """A card the user is shown (`card`) or a change to one (`card.update`), M4.

    Both are trace records with a seq, so the run's SSE stream replays them; the web
    client (and the eval harness, which reads cards the way the client does) never
    needs anything else to draw the card or grey it out.
    """

    type: Literal["card", "card.update"]
    approval_id: str | None = None
    message_id: str | None = None
    body: dict[str, Json] | None = None
    status: str | None = None
    reason: str | None = None
    # card.update after a brief correction (M8): the brief fields that changed, so the
    # live UI can say what made the card stale.
    changed_fields: list[str] | None = None


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
    | CardRecord
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


class ExportHeader(BaseModel):
    """First record of `errand-trace export` (N4, 16.3). Written by the exporter, never by
    a Tracer, so it is not in `RECORD_TYPES` and carries no span envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    type: Literal["export.header"]
    thread_id: str
    run_ids: list[str]
    # The code that produced the runs: `run_records` when the run.start records name it,
    # else `exporting_checkout` (the SHA of the checkout that ran the export).
    git_sha: str | None
    git_sha_source: Literal["run_records", "exporting_checkout"]
    model_mode: str  # the `mode` of the model.call records, or "none" when there are none
    clock_mode: Literal["sim", "system_offset"]
    exported_at: str


class ConversationTotals(BaseModel):
    """Last record of an export: the run totals again, summed across the file's runs from
    its own records (Decimal for cost, as the tracer sums run.end)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    type: Literal["conversation.totals"]
    thread_id: str
    runs: NonNegativeInt
    model_calls: NonNegativeInt
    reader_calls: NonNegativeInt
    tool_calls: NonNegativeInt
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    cost_usd: NonNegativeFloat


# What an exported file may hold: every trace record plus the two export records.
EXPORT_ADAPTER: TypeAdapter[TraceRecord | ExportHeader | ConversationTotals] = TypeAdapter(
    Annotated[
        TraceRecord | ExportHeader | ConversationTotals,
        Field(discriminator="type"),
    ]
)

# Records that must survive a crash of the process right after they were written.
FSYNC_PREFIXES = ("approval.", "intent.")
FSYNC_TYPES = frozenset({"run.end"})


def needs_fsync(record_type: str) -> bool:
    return record_type in FSYNC_TYPES or record_type.startswith(FSYNC_PREFIXES)
