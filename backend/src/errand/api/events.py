"""The run event stream as OpenAPI components (architecture 17.4).

`GET /api/runs/{id}/events` is Server-Sent Events, which OpenAPI cannot describe as a
response model, so these models are added to `components.schemas` by `add_components`
and the web client's types for events come from the generated file like the cards do.

Every replayable event carries a trace record as its data (and `id: <seq>`); `token`
and `error` do not. The wire form is unchanged by this module: the models document it,
`api/sse.py` still builds the events.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema

from errand.domain.messages import MessageBody


class TraceRecordData(BaseModel):
    """A trace record as it reaches the client: the v1 envelope, plus the fields of its
    `type` (schema v1, trace/schema.py), which the client reads by name."""

    model_config = ConfigDict(extra="allow")

    v: int
    seq: int
    ts: str
    wall_ts: str
    run_id: str
    thread_id: str
    errand_id: str | None = None
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    node_attempt: int = 1
    type: str


class CardRecordData(TraceRecordData):
    """`card` and `card.update` records: the body is a message body (domain/messages)."""

    approval_id: str | None = None
    message_id: str | None = None
    body: MessageBody | None = None
    status: str | None = None
    reason: str | None = None


class RunStartedEvent(BaseModel):
    event: Literal["run.started"] = "run.started"
    id: str
    data: TraceRecordData


class RunFinishedEvent(BaseModel):
    event: Literal["run.finished"] = "run.finished"
    id: str
    data: TraceRecordData


class TraceEvent(BaseModel):
    """Every other trace record: tool.call, model.call, node.start and so on. A type the
    client does not know is shown nowhere and never an error."""

    event: Literal["trace"] = "trace"
    id: str
    data: TraceRecordData


class CardEvent(BaseModel):
    event: Literal["card"] = "card"
    id: str
    data: CardRecordData


class CardUpdateEvent(BaseModel):
    event: Literal["card.update"] = "card.update"
    id: str
    data: CardRecordData


class TokenData(BaseModel):
    text: str


class TokenEvent(BaseModel):
    """Live text. No id and never replayed: after a reconnect the transcript is the truth."""

    event: Literal["token"] = "token"
    data: TokenData


class StreamErrorData(BaseModel):
    error: str
    run_id: str


class StreamErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    data: StreamErrorData


RunEvent = Annotated[
    RunStartedEvent
    | RunFinishedEvent
    | TraceEvent
    | CardEvent
    | CardUpdateEvent
    | TokenEvent
    | StreamErrorEvent,
    Field(discriminator="event"),
]

EVENT_MODELS: tuple[type[BaseModel], ...] = (
    TraceRecordData,
    CardRecordData,
    RunStartedEvent,
    RunFinishedEvent,
    TraceEvent,
    CardEvent,
    CardUpdateEvent,
    TokenEvent,
    StreamErrorEvent,
)


def add_components(schema: dict[str, Any]) -> dict[str, Any]:
    """Merge the event models into an OpenAPI document's `components.schemas`, plus a
    `RunEvent` union with its discriminator."""
    _, definitions = models_json_schema(
        [(m, "serialization") for m in EVENT_MODELS], ref_template="#/components/schemas/{model}"
    )
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in definitions.get("$defs", {}).items():
        schemas.setdefault(name, definition)
    members: list[tuple[str, type[BaseModel]]] = [
        ("run.started", RunStartedEvent),
        ("run.finished", RunFinishedEvent),
        ("trace", TraceEvent),
        ("card", CardEvent),
        ("card.update", CardUpdateEvent),
        ("token", TokenEvent),
        ("error", StreamErrorEvent),
    ]
    schemas["RunEvent"] = {
        "oneOf": [{"$ref": f"#/components/schemas/{m.__name__}"} for _, m in members],
        "discriminator": {
            "propertyName": "event",
            "mapping": {name: f"#/components/schemas/{m.__name__}" for name, m in members},
        },
        "title": "RunEvent",
        "description": "One Server-Sent Event of GET /api/runs/{run_id}/events.",
    }
    return schema
