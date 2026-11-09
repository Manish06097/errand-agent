"""Evidence for an explicit write, the n-gram mask, the brief scrub and the span edits."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from errand.domain.brief import AreaFact, Brief, BudgetFact, Provenance
from errand.memory import hmac_key, redaction
from errand.memory.evidence import EvidenceRefused, check_explicit, locate_quote
from errand.memory.mask import hash_of, mask_text, normal_form
from errand.memory.scrub import scrub_brief, scrub_brief_json, stale_fields
from errand.world.contracts import Area

KEY = hmac_key.fixed_key("tests")


def _check(message: str, quote: str, key: str = "earliest_time", value: object = "10:00"):  # type: ignore[no-untyped-def]
    scope = "global" if key != "budget_ceiling" else "service:plumber"
    return check_explicit(message=message, quote=quote, key=key, scope=scope, value=value)


# ------------------------------------------------------------------------------ evidence


def test_a_cue_bearing_quote_with_the_value_is_accepted_with_original_offsets() -> None:
    message = "Hi!  I always want to start after 10 am, thanks"
    got = _check(message, "i ALWAYS want to start after 10 AM")
    assert message[got.start : got.end] == got.quote == "I always want to start after 10 am"
    assert got.cue == "always"


def test_whitespace_and_case_differences_still_locate_the_quote() -> None:
    located = locate_quote("never   book\nbefore ten", "Never book before ten")
    assert located is not None and located.quote == "never   book\nbefore ten"


@pytest.mark.parametrize(
    ("message", "quote", "reason"),
    [
        ("always ten", "abc", "quote_too_short"),
        ("always before 10 am", "always at 11 am", "quote_not_in_message"),
        ("this time I prefer after 10 am", "this time I prefer after 10 am", "one_off"),
        ("I want it after 10 am please", "after 10 am", "no_cue"),
        ("I always book mornings", "I always book mornings", "value_not_in_quote"),
    ],
)
def test_each_missing_piece_of_evidence_is_refused_with_its_reason(
    message: str, quote: str, reason: str
) -> None:
    with pytest.raises(EvidenceRefused) as err:
        _check(message, quote)
    assert err.value.reason == reason


@pytest.mark.parametrize(
    ("message", "quote", "key", "value"),
    [
        # decision 3's own example, quoted narrowly out of the one-off sentence
        ("this time I prefer after 5 pm", "I prefer after 5 pm", "earliest_time", "17:00"),
        ("For now I live in Koramangala", "I live in Koramangala", "home_area", "koramangala"),
        (
            "Just for this booking: I always want mornings, never before 10 am",
            "never before 10 am",
            "earliest_time",
            "10:00",
        ),
        (
            "Just for this booking: I always want mornings, never before 10 am",
            "I always want mornings, never before 10 am",
            "earliest_time",
            "10:00",
        ),
        (
            "Thanks! Today I always want it after 10 am\nok",
            "I always want it after 10 am",
            "earliest_time",
            "10:00",
        ),
    ],
)
def test_the_one_off_veto_reads_the_whole_sentence_not_just_the_quote(
    message: str, quote: str, key: str, value: object
) -> None:
    with pytest.raises(EvidenceRefused) as err:
        _check(message, quote, key, value)
    assert err.value.reason == "one_off"


@pytest.mark.parametrize(
    "message",
    [
        "Book me a plumber today. I always want to start after 10 am",  # veto in another sentence
        "This time is fine; I always want to start after 10 am",  # another clause
        "I have 10.5 hours free today\nI always want to start after 10 am",  # another line
    ],
)
def test_a_veto_in_another_sentence_does_not_block_a_standing_preference(message: str) -> None:
    got = _check(message, "I always want to start after 10 am")
    assert got.cue == "always"


def test_the_cue_must_still_be_inside_the_quote() -> None:
    # the cue sits outside the quote: quoting the value alone is not evidence
    with pytest.raises(EvidenceRefused) as err:
        _check("I always want to start after 10 am", "start after 10 am")
    assert err.value.reason == "no_cue"


def test_a_quote_the_user_never_wrote_is_refused_even_if_it_has_the_cue() -> None:
    # S16: a review's text is not the user's message.
    with pytest.raises(EvidenceRefused) as err:
        _check("book a plumber tomorrow", "I always want it before 10 am")
    assert err.value.reason == "quote_not_in_message"


# ------------------------------------------------------------------------------- the key


def test_the_hmac_key_is_created_once_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "data" / "memory_hmac.key"
    first = hmac_key.load_or_create(path)
    assert len(first) == 32 and hmac_key.load_or_create(path) == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


# -------------------------------------------------------------------------------- the mask


def _hashes(*texts: str) -> dict[str, str]:
    return {h: f"m-{i}" for i, t in enumerate(texts) if (h := hash_of(KEY, t)) is not None}


def test_normal_form_ignores_case_punctuation_and_spacing() -> None:
    assert normal_form("  Not BEFORE, ten!!  ") == normal_form("not before ten")
    assert normal_form("10 a.m.") == normal_form("10 A.M.")


def test_the_mask_replaces_a_surface_form_and_counts_it() -> None:
    result = mask_text("Please come at 10 AM tomorrow", KEY, _hashes("10 am"))
    assert result.text == "Please come at [removed] tomorrow"
    assert result.masked == 1 and result.memory_ids == ("m-0",)


def test_the_mask_prefers_the_longest_match_and_never_overlaps() -> None:
    hashes = _hashes("not before ten", "ten")
    result = mask_text("I said not before ten. Ten is fine.", KEY, hashes)
    assert result.text == "I said [removed]. [removed] is fine."
    assert result.memory_ids == ("m-0", "m-1")


def test_the_mask_finds_a_whole_redacted_span_inside_longer_text() -> None:
    span = "I always want to start after ten in the morning"
    result = mask_text(f"Earlier: {span}. Noted.", KEY, _hashes(span))
    assert "ten" not in result.text and result.masked == 1


def test_no_hashes_or_no_match_leaves_text_untouched() -> None:
    assert mask_text("hello 10:00", KEY, {}).text == "hello 10:00"
    got = mask_text("hello 11:00", KEY, _hashes("10:00"))
    assert got.text == "hello 11:00" and got.masked == 0


def test_a_different_key_hashes_differently() -> None:
    other = hmac_key.fixed_key("other")
    assert hash_of(KEY, "10 am") != hash_of(other, "10 am")


# ------------------------------------------------------------------------------- the scrub


def _prov(memory_id: str | None, evidence: str = "I live in Indiranagar") -> Provenance:
    return Provenance(source="memory", memory_id=memory_id, evidence=evidence)


def _brief() -> Brief:
    return Brief(
        area=AreaFact(value=Area.INDIRANAGAR, prov=_prov("m-01"), set_at_turn=1),
        budget_max_inr=BudgetFact(value=800, prov=_prov("m-02", "under 800"), set_at_turn=1),
    )


def test_a_field_from_a_memory_that_is_not_eligible_is_dropped() -> None:
    brief, cleared = scrub_brief(_brief(), {"m-02"})
    assert cleared == ["area"] and brief.area is None and brief.budget_max_inr is not None


def test_a_field_with_no_memory_id_is_never_touched() -> None:
    brief = Brief(area=AreaFact(value=Area.INDIRANAGAR, prov=_prov(None), set_at_turn=1))
    assert stale_fields(brief, set()) == []
    assert scrub_brief(brief, set())[0] is brief


def test_the_stored_json_form_gives_the_same_answer_and_clears_the_evidence_with_it() -> None:
    raw = _brief().model_dump_json()
    scrubbed, cleared = scrub_brief_json(raw, {"m-02"})
    assert cleared == ["area"]
    data = json.loads(scrubbed)
    assert data["area"] is None and "Indiranagar" not in scrubbed
    assert data["budget_max_inr"]["prov"]["memory_id"] == "m-02"
    assert scrub_brief_json(raw, {"m-01", "m-02"}) == (raw, [])


# ------------------------------------------------------------------------------ span edits


def test_overwrite_replaces_merged_spans_and_reports_where_the_markers_sit() -> None:
    text = "ten, then ten again"
    spans = redaction.find_spans(text, ["ten"])
    assert spans == [(0, 3), (10, 13)]
    out = redaction.overwrite(text, spans, "[x]")
    assert out == "[x], then [x] again"
    assert [out[a:b] for a, b in redaction.new_spans(spans, "[x]")] == ["[x]", "[x]"]


def test_find_spans_is_word_bounded_and_prefers_the_longer_form() -> None:
    assert redaction.find_spans("often ten", ["ten"]) == [(6, 9)]
    assert redaction.find_spans("10 am sharp", ["10", "10 am"]) == [(0, 5)]


def test_an_existing_redaction_row_moves_with_an_earlier_edit_or_is_swallowed() -> None:
    assert redaction.shift_after((20, 45), [(0, 3)], 25) == (42, 67)
    assert redaction.shift_after((2, 8), [(0, 3)], 25) is None
    assert redaction.shift_after((0, 5), [(10, 13)], 25) == (0, 5)


def test_redact_body_overwrites_primary_text_and_options() -> None:
    body = {
        "type": "clarify",
        "question": "Is 10 am still right for Indiranagar?",
        "options": ["Yes, 10 am", "No"],
        "status_line": "Not booked yet",
    }
    new, edited = redaction.redact_body(body, ["10 am"])
    assert new["status_line"] == "Not booked yet"
    assert new["question"] == "Is [removed at your request] still right for Indiranagar?"
    assert new["options"] == ["Yes, [removed at your request]", "No"]
    assert edited == [(3, 8)] and str(body["question"]).startswith("Is 10 am")


def test_redact_body_leaves_code_rendered_text_alone() -> None:
    """Decision 13: redaction covers what the user and the model wrote, never the text code
    renders (a status line, an outcome title, a booking line). Forgetting 'not before 10'
    must not wipe '10:00' from the record of a booking."""
    status = "Waiting for you: 10 am slot held"
    for body in (
        {"type": "outcome", "text": "Booked for 10 am", "title": "Booked 10 am", "lines": []},
        {"type": "reminder", "text": "Still waiting on: 10 am", "status_line": status},
        {"type": "text", "text": "Fine.", "status_line": status},
    ):
        new, edited = redaction.redact_body(body, ["10 am"])
        assert new == body and edited == [], body["type"]
    # a notice is redacted (a step-limit notice restates the brief), its status line is not
    notice, _ = redaction.redact_body(
        {"type": "notice", "text": "Nothing at 10 am today", "status_line": status}, ["10 am"]
    )
    assert notice["text"] == "Nothing at [removed at your request] today"
    assert notice["status_line"] == status
    # what the model wrote in a card is still redacted
    card, _ = redaction.redact_body(
        {"type": "shortlist", "recommendation": "Take the 10 am one", "status_line": status},
        ["10 am"],
    )
    assert card["recommendation"] == "Take the [removed at your request] one"
    assert card["status_line"] == status


def test_the_model_view_hides_a_superseded_span() -> None:
    text = "I live in Indiranagar, book a plumber"
    assert (
        redaction.apply_model_view(text, [(10, 21, "superseded")])
        == "I live in [updated preference], book a plumber"
    )
