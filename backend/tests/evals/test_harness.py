"""The harness itself: the DSL rejects what it does not know, and SimUser catches a re-ask."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from errand.llm.scripted import ScriptedRequest
from evals.dsl import load_scenario
from evals.policies import reference
from evals.runner import EVALS, run_scenario

S03 = EVALS / "scenarios" / "S03_terse_four_turns.yaml"


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body)
    return path


BASE = """
id: {id}
title: t
category: [conversation]
user: {{persona: cooperative}}
steps:
{steps}
"""


def test_an_unknown_step_verb_fails_at_load(tmp_path: Path) -> None:
    # crash_at is an M6 verb; until it is built, a scenario using it must not load.
    path = write(tmp_path, "X1", BASE.format(id="X1", steps="  - crash_at: {}"))
    with pytest.raises(ValidationError, match="exactly one verb"):
        load_scenario(path)


def test_an_unknown_key_on_a_step_fails_at_load(tmp_path: Path) -> None:
    path = write(tmp_path, "X2", BASE.format(id="X2", steps='  - say: "hi"\n    givs: [area]'))
    with pytest.raises(ValidationError, match=r"exactly one verb|givs"):
        load_scenario(path)


def test_an_unknown_fault_fails_at_load(tmp_path: Path) -> None:
    body = BASE.format(id="X3", steps='  - say: "hi"') + "faults: {search_provider: 503}\n"
    with pytest.raises(ValidationError, match="search_provider"):
        load_scenario(write(tmp_path, "X3", body))


def test_the_id_must_match_the_file_name(tmp_path: Path) -> None:
    path = write(tmp_path, "X4", BASE.format(id="other", steps='  - say: "hi"'))
    with pytest.raises(ValueError, match="must match the file name"):
        load_scenario(path)


def forgets_the_area(request: ScriptedRequest) -> AIMessage:
    """The reference policy, except that it never records the user's area."""
    message = reference.policy(request)
    for call in message.tool_calls:
        if call["name"] == "update_brief":
            call["args"]["set"]["area"] = None
            call["args"]["evidence"] = [e for e in call["args"]["evidence"] if e["field"] != "area"]
    for block in message.content:
        if isinstance(block, dict) and block.get("name") == "update_brief":
            block["input"] = next(c["args"] for c in message.tool_calls)
    return message


async def test_simuser_fails_the_scenario_when_asked_for_a_field_it_gave(tmp_path: Path) -> None:
    # The guard allows this re-ask (the area is still missing, and options are offered),
    # so only the simulated user's own memory catches that the agent lost its answer.
    scenario = load_scenario(S03)
    result = await run_scenario(scenario, tmp_path, policy=forgets_the_area)
    assert not result.passed
    assert "simulated user: asked again for area, which the user already gave" in result.failures


async def test_s03_passes_with_the_reference_policy(tmp_path: Path) -> None:
    result = await run_scenario(load_scenario(S03), tmp_path)
    assert result.passed, result.failures
