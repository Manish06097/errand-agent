"""Every stop_reason maps to a typed result through the real API (architecture 10.4, 15.2,
N3 part A step 6): refusal ends MODEL_REFUSED with no retry, max_tokens spends the run's
one reprompt on a byte-identical resend and then ends MODEL_NO_PROGRESS, and anything we
never ask for is INTERNAL_ERROR with an `error` record naming it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedChatModel
from evals import invariants
from evals.runner import harness
from tests.api.support import OPENER, world

USAGE = {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}


def stopped(reason: str, text: str = "partial") -> AIMessage:
    return AIMessage(
        content=[{"type": "text", "text": text}],
        usage_metadata=USAGE,
        response_metadata={"stop_reason": reason, "model_name": "claude-opus-5-5"},
    )


def factory(script: list[AIMessage]) -> Any:
    model = ScriptedChatModel(script=script)
    return lambda: model


async def run(tmp_path: Path, script: list[AIMessage]) -> tuple[Any, Any, ScriptedChatModel]:
    model = ScriptedChatModel(script=script)
    async with harness(tmp_path, world(), model_factory=lambda: model) as h:
        view = await h.say(OPENER)
        rec = await h.records()
    return view, rec, model


async def test_a_refusal_ends_model_refused_with_no_retry(tmp_path: Path) -> None:
    view, rec, model = await run(tmp_path, [stopped("refusal", "")])
    assert view.outcome == "MODEL_REFUSED"
    notice = view.notice()
    assert notice is not None and notice["text"].startswith("I can't help with that request.")
    assert len(model.requests) == 1
    assert invariants.inv07_one_end_per_run(rec) == []


async def test_max_tokens_is_retried_once_with_the_same_request(tmp_path: Path) -> None:
    done = AIMessage(
        content=[{"type": "text", "text": "Which area is that in?"}],
        usage_metadata=USAGE,
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5"},
    )
    view, rec, model = await run(tmp_path, [stopped("max_tokens"), done])
    assert view.outcome == "REPLIED"
    assert len(model.requests) == 2
    assert model.requests[0].canonical == model.requests[1].canonical  # byte for byte
    # The cut-off answer never went back to the model.
    assert "partial" not in str(model.requests[1].messages)
    reprompts = [r for r in rec.traces[view.run_id] if r["type"] == "guard.reprompt"]
    assert [r["detail"]["reason"] for r in reprompts] == ["max_tokens"]


async def test_a_second_max_tokens_ends_model_no_progress(tmp_path: Path) -> None:
    view, rec, model = await run(tmp_path, [stopped("max_tokens"), stopped("max_tokens")])
    assert view.outcome == "MODEL_NO_PROGRESS"
    assert len(model.requests) == 2
    assert invariants.inv07_one_end_per_run(rec) == []
    assert invariants.check(rec)["INV-09"] == []


@pytest.mark.parametrize("reason", ["pause_turn", "stop_sequence", "something_new"])
async def test_a_stop_reason_we_never_ask_for_is_internal_error_and_named(
    reason: str, tmp_path: Path
) -> None:
    view, rec, model = await run(tmp_path, [stopped(reason)])
    assert view.outcome == "INTERNAL_ERROR"
    errors = [r for r in rec.traces[view.run_id] if r["type"] == "error"]
    assert any(reason in e["message"] for e in errors)
    assert len(model.requests) == 1
