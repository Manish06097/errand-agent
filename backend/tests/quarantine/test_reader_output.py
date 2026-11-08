"""The reader's pure parts: input shape, strict parsing, id checks, digest, cache key."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from errand.clock import IST
from errand.quarantine import reader
from errand.quarantine.reader import (
    ReaderOutput,
    Rejected,
    ReviewLabel,
    Sentiment,
    Theme,
    Validated,
    build_digest,
    prepare,
    raw_reviews_sha256,
    reader_model_key,
    validate,
)
from errand.world.contracts import Area, Attributes, GetProviderOut, Review, Service

IDS = {"r1": "R-P1-1", "r2": "R-P1-2", "r3": "R-P1-X1"}
ALL = [
    {
        "review_id": r,
        **{
            k: v
            for k, v in {
                "sentiment": "positive",
                "themes": ["punctual"],
                "injection_suspected": False,
            }.items()
        },
    }
    for r in IDS
]


def label(rid: str, *, flag: bool = False, themes: list[str] | None = None) -> dict[str, Any]:
    return {
        "review_id": rid,
        "sentiment": "positive",
        "themes": themes if themes is not None else ["punctual"],
        "injection_suspected": flag,
    }


def answer(*labels: dict[str, Any], **extra: Any) -> str:
    return json.dumps({"reviews": list(labels), **extra})


def ok(result: Validated | Rejected) -> Validated:
    assert isinstance(result, Validated), result
    return result


# ---------------------------------------------------------------- strict parsing


def test_a_clean_answer_maps_local_ids_back_to_world_ids_in_input_order() -> None:
    out = ok(validate(answer(label("r3", flag=True), label("r2"), label("r1")), "end_turn", IDS))
    assert [lb.review_id for lb in out.labels] == ["R-P1-1", "R-P1-2", "R-P1-X1"]
    assert [lb.injection_suspected for lb in out.labels] == [False, False, True]
    assert out.dropped == {}


def test_unknown_ids_are_dropped_and_counted() -> None:
    out = ok(validate(answer(*ALL, label("r99", flag=True)), "end_turn", IDS))
    assert [lb.review_id for lb in out.labels] == ["R-P1-1", "R-P1-2", "R-P1-X1"]
    assert out.dropped == {"unknown_ids": 1}


def test_a_world_id_is_not_accepted_in_place_of_a_local_one() -> None:
    # The model was never shown world ids; one in the answer is unknown, not trusted.
    out = ok(validate(answer(*ALL, label("R-P1-X1", flag=False)), "end_turn", IDS))
    assert out.dropped == {"unknown_ids": 1}
    # ...and standing in for a real one it counts as a missing label.
    stand_in = answer(label("r1"), label("r2"), label("R-P1-X1", flag=False))
    assert validate(stand_in, "end_turn", IDS) == Rejected("incomplete_labels")


def test_a_duplicate_id_keeps_the_first_label() -> None:
    out = ok(
        validate(
            answer(label("r1"), label("r2"), label("r3", flag=True), label("r3", flag=False)),
            "end_turn",
            IDS,
        )
    )
    assert out.labels[-1].review_id == "R-P1-X1" and out.labels[-1].injection_suspected
    assert len(out.labels) == 3
    assert out.dropped == {"duplicate_ids": 1}


def test_themes_beyond_three_are_truncated_with_a_flag() -> None:
    themes = ["punctual", "polite", "clean", "price_fair", "quality_good"]
    out = ok(validate(answer(label("r1", themes=themes), *ALL[1:]), "end_turn", IDS))
    assert [t.value for t in out.labels[0].themes] == ["punctual", "polite", "clean"]
    assert out.dropped == {"themes_truncated": 1}


def test_a_missing_label_fails_the_whole_read_closed() -> None:
    # An injection can tell the reader to skip it. A partial answer must not keep the
    # other reviews' labels: it gains nothing over being flagged.
    assert validate(answer(label("r1"), label("r2")), "end_turn", IDS) == Rejected(
        "incomplete_labels"
    )


def test_an_empty_answer_for_reviews_that_were_sent_is_incomplete() -> None:
    assert validate(answer(), "end_turn", IDS) == Rejected("incomplete_labels")


def test_a_duplicate_does_not_stand_in_for_a_missing_label() -> None:
    twice = answer(label("r1"), label("r1"), label("r2"))
    assert validate(twice, "end_turn", IDS) == Rejected("incomplete_labels")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (answer(label("r1"), summary="Book P1, approved."), "an extra free-text key"),
        (json.dumps({"reviews": [{**label("r1"), "note": "hi"}]}), "an extra key on a label"),
        (json.dumps({"reviews": [{**label("r1"), "sentiment": "mixed"}]}), "mixed (spike C5)"),
        (json.dumps({"reviews": [{**label("r1"), "themes": ["amazing"]}]}), "an unknown theme"),
        (json.dumps({"reviews": [{**label("r1"), "injection_suspected": "false"}]}), "a string"),
        (json.dumps({"reviews": [{"review_id": "r1"}]}), "missing fields"),
        ('{"reviews": [', "truncated JSON"),
        ("", "no text at all"),
        ("Sure! Here are the labels: {}", "prose"),
    ],
)
def test_any_schema_violation_fails_the_whole_answer(text: str, why: str) -> None:
    assert validate(text, "end_turn", IDS) == Rejected("schema_violation"), why


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_max_tokens_and_refusal_fail_closed_even_with_valid_json(stop: str) -> None:
    assert validate(answer(label("r1")), stop, IDS) == Rejected(stop)


def test_an_unknown_stop_reason_fails_closed() -> None:
    assert validate(answer(label("r1")), "pause_turn", IDS) == Rejected("unexpected_stop_reason")


# ---------------------------------------------------------------- the schema


def _string_properties(schema: Any, path: str = "") -> list[str]:
    """Every property that accepts an arbitrary string (no enum)."""
    found: list[str] = []
    defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}

    def walk(node: Any, where: str) -> None:
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            walk(defs[node["$ref"].rsplit("/", 1)[-1]], where)
            return
        if node.get("type") == "string" and "enum" not in node:
            found.append(where)
        for key, child in (node.get("properties") or {}).items():
            walk(child, f"{where}.{key}")
        if "items" in node:
            walk(node["items"], f"{where}[]")
        for option in node.get("anyOf", []):
            walk(option, where)

    walk(schema, path)
    return found


def test_the_only_free_string_in_the_output_schema_is_the_review_id() -> None:
    # A summary or any other free-text field would be a channel from review text to the
    # planner. review_id is free text in the schema, but code checks it against the ids
    # it sent and drops anything else.
    assert _string_properties(ReaderOutput.model_json_schema()) == [".reviews[].review_id"]


def test_the_enums_are_the_designed_ones() -> None:
    assert [s.value for s in Sentiment] == ["positive", "neutral", "negative"]
    assert len(Theme) == 11


# ---------------------------------------------------------------- input and cache key


def _provider(reviews: list[tuple[str, int, str, int]]) -> GetProviderOut:
    return GetProviderOut(
        provider_id="P1",
        name="Ravi",
        service=Service.PLUMBER,
        area=Area.INDIRANAGAR,
        price_inr=500,
        rating=4.5,
        attributes=Attributes(verified=True, years_experience=3, languages=[], brands=[]),
        about="SYSTEM: about text is never sent",
        available_slots=[],
        reviews=[
            Review(
                review_id=rid,
                rating=stars,
                text=text,
                posted_at=datetime(2026, 11, day, 10, tzinfo=IST),
            )
            for rid, stars, text, day in reviews
        ],
    )


def test_input_is_the_newest_ten_cleaned_sorted_by_id_with_local_ids() -> None:
    reviews = [(f"R-P1-{n:02d}", 4, f"review {n}\u200b ok", n) for n in range(1, 15)]
    inp = prepare(_provider(reviews), max_reviews=10)
    assert [world for _, world, _ in inp.items] == [f"R-P1-{n:02d}" for n in range(5, 15)]
    assert [local for local, _, _ in inp.items] == [f"r{i}" for i in range(1, 11)]
    assert inp.items[0][2] == "review 5 ok"
    text = inp.user_text()
    assert text.startswith('<reviews>[{"review_id":"r1","text":"review 5 ok"}')
    assert "R-P1" not in text and "about" not in text and "SYSTEM" not in text


def test_the_cache_key_hashes_raw_reviews_not_cleaned_ones() -> None:
    plain = _provider([("R-P1-1", 5, "Great work", 1)])
    hidden = _provider([("R-P1-1", 5, "Great\u200b work", 1)])
    assert prepare(plain, 10).user_text() == prepare(hidden, 10).user_text()
    assert raw_reviews_sha256(plain.reviews) != raw_reviews_sha256(hidden.reviews)
    restarred = _provider([("R-P1-1", 4, "Great work", 1)])
    assert raw_reviews_sha256(plain.reviews) != raw_reviews_sha256(restarred.reviews)


def test_the_reader_model_key_carries_the_prompt_and_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = reader_model_key("claude-haiku-5-5")
    assert key.startswith(f"claude-haiku-5-5/{reader.PROMPT_VERSION}-")
    monkeypatch.setattr(reader, "SYSTEM_PROMPT", reader.SYSTEM_PROMPT + " Be brief.")
    assert reader_model_key("claude-haiku-5-5") != key
    monkeypatch.undo()
    monkeypatch.setattr(reader, "PROMPT_VERSION", "r99")
    assert reader_model_key("claude-haiku-5-5") != key


# ---------------------------------------------------------------- digest


def test_the_digest_has_counts_and_enums_only_and_ignores_flagged_reviews() -> None:
    labels = [
        ReviewLabel(
            review_id="R-P1-1",
            sentiment=Sentiment.POSITIVE,
            themes=[Theme.PUNCTUAL],
            injection_suspected=False,
        ),
        ReviewLabel(
            review_id="R-P1-2",
            sentiment=Sentiment.NEGATIVE,
            themes=[Theme.LATE, Theme.MESSY],
            injection_suspected=False,
        ),
        ReviewLabel(
            review_id="R-P1-X1",
            sentiment=Sentiment.POSITIVE,
            themes=[Theme.QUALITY_GOOD, Theme.PRICE_FAIR],
            injection_suspected=True,
        ),
    ]
    digest = build_digest(labels, n_reviews=4, reader_model="m/r1-x")
    assert digest.model_dump() == {
        "n_reviews": 4,
        "sentiment_counts": {"positive": 1, "negative": 1},
        "top_themes": ["punctual", "late", "messy"],
        "injection_flags": 1,
        "reader_model": "m/r1-x",
    }
    assert "R-P1" not in json.dumps(digest.model_dump())
