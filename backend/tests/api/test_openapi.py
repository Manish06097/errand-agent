"""M7 generates its TypeScript types from the OpenAPI document (17.4): the card models
must be components, not anonymous dicts."""

from __future__ import annotations

from errand.api.app import app


def test_the_card_models_are_named_components() -> None:
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "ActionCard",
        "CardReminder",
        "OutcomeNotice",
        "OutcomeCard",
        "ShortlistCard",
        "ClarifyCard",
        "TextBody",
        "ApprovalStatusOut",
        "ConversationOut",
        "MessageOut",
        "ClockOut",
        "RunEvent",
        "CardEvent",
        "TraceOut",
    ):
        assert name in schemas, name
    body = schemas["MessageOut"]["properties"]["body"]
    assert body["discriminator"]["propertyName"] == "type"
    assert len(body["oneOf"]) == 14
