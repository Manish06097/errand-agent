"""The SSE event union is part of the OpenAPI document (17.4), so the web client's event
types are generated like its card types."""

from __future__ import annotations

from errand.api.app import app


def test_the_event_models_are_components() -> None:
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "RunEvent",
        "RunStartedEvent",
        "RunFinishedEvent",
        "TraceEvent",
        "CardEvent",
        "CardUpdateEvent",
        "TokenEvent",
        "StreamErrorEvent",
        "TraceRecordData",
        "CardRecordData",
        "TraceOut",
        "ConversationSummary",
        "MetaOut",
    ):
        assert name in schemas, name
    union = schemas["RunEvent"]
    assert union["discriminator"]["propertyName"] == "event"
    assert set(union["discriminator"]["mapping"]) == {
        "run.started",
        "run.finished",
        "trace",
        "card",
        "card.update",
        "token",
        "error",
    }
    # A card event carries a typed message body (the eight message bodies), not a loose dict.
    body = schemas["CardRecordData"]["properties"]["body"]
    union = next(option for option in body["anyOf"] if "oneOf" in option)
    assert len(union["oneOf"]) == 8
    assert union["discriminator"]["propertyName"] == "type"


def test_openapi_is_built_once_and_stable() -> None:
    assert app.openapi() is app.openapi()
