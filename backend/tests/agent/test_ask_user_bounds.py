"""The ClarifyCard is the one place model text reaches the user outside REPLIED and the
recommendation line, so its question and options are bounded and cleaned in code.

A scripted planner first sends seven options (refused as a tool error), then a question
and options carrying zero-width characters, a phone number and a duplicate. The card the
API persists is what the user sees, so the assertions read it from the transcript.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage

from errand.agent.planner_tools import ASK_OPTION_MAX_CHARS, ASK_OPTIONS_MAX
from errand.llm.scripted import ScriptedRequest
from evals.policies.common import read, tool_calls
from evals.runner import harness
from tests.api.support import world

FIELDS = ["service", "area", "window", "budget_max_inr"]
ZWSP = "​"


def noisy_asker(request: ScriptedRequest) -> AIMessage:
    step = len(read(request).loop)
    if step == 0:
        too_many = [f"Option {i}" for i in range(ASK_OPTIONS_MAX + 1)]
        return tool_calls(
            request,
            "Ask with every option I can think of.",
            ("ask_user", {"fields": FIELDS, "question": "What do you need?", "options": too_many}),
        )
    return tool_calls(
        request,
        "Ask again, within the limits.",
        (
            "ask_user",
            {
                "fields": FIELDS,
                "question": f"What{ZWSP} do you need?   Call me on 98450 12345.",
                "options": [f"Plumber{ZWSP}", "Plumber", "  Electrician  ", "x" * 40],
            },
        ),
    )


async def test_ask_user_options_are_capped_and_the_card_is_cleaned(tmp_path: Path) -> None:
    async with harness(tmp_path, world("ask_bounds"), policy=noisy_asker) as h:
        view = await h.say("hi")
        records = await h.records()

    assert view.outcome == "ASKED_USER"
    card = view.card()
    assert card is not None
    assert ZWSP not in card.question
    assert "98450" not in card.question and "12345" not in card.question
    assert "  " not in card.question
    assert card.options == ["Plumber", "Electrician", "x" * 40]
    assert len(card.options) <= ASK_OPTIONS_MAX
    assert all(len(o) <= ASK_OPTION_MAX_CHARS for o in card.options)

    # The seven-option call was a tool error the model saw, not a card.
    loop_results = [
        b["content"]
        for recs in records.traces.values()
        for r in recs
        if r["type"] == "model.call"
        for m in r["request"]["messages"][-1:]
        if isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]
    assert any('"error":"invalid_arguments"' in str(c) for c in loop_results), loop_results


async def test_an_option_over_the_length_cap_is_refused(tmp_path: Path) -> None:
    def long_option(request: ScriptedRequest) -> AIMessage:
        step = len(read(request).loop)
        options = ["y" * (ASK_OPTION_MAX_CHARS + 1)] if step == 0 else ["Plumber"]
        return tool_calls(
            request,
            "Ask.",
            ("ask_user", {"fields": FIELDS, "question": "What do you need?", "options": options}),
        )

    async with harness(tmp_path, world("ask_bounds_len"), policy=long_option) as h:
        view = await h.say("hi")

    card = view.card()
    assert card is not None and card.options == ["Plumber"]
