"""The planner's tool schemas obey strict tool use, and only read tools are listed.

Scripted mode accepts any schema; the API does not. These rules are the ones strict
tool use enforces on an input schema: every object closes with `additionalProperties:
false` and lists every property in `required` (so optional values are nullable), and
the schema keeps to the supported keywords. The empty object of search_providers and
list_bookings must pass too.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage

from errand.agent.planner_tools import PLANNER_TOOL_NAMES, PLANNER_TOOLS
from errand.config import ModelSettings
from errand.context.builder import CAPS
from errand.llm.gateway import LLMGateway
from errand.llm.scripted import ScriptedChatModel
from errand.llm.tokens import TOOLS_PREAMBLE_TOKENS, est_tokens
from errand.tools.gateway import CONSEQUENTIAL_TOOLS
from errand.trace.sinks.null import NullSink
from errand.trace.tracer import Tracer

SUPPORTED = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
    "description",
}
UNSUPPORTED = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "pattern",
    "minItems",
    "maxItems",
    "default",
    "$ref",
    "oneOf",
}


def strict_problems(schema: Any, where: str) -> list[str]:
    problems: list[str] = []
    if not isinstance(schema, dict):
        return [f"{where}: not a schema object"]
    keys = set(schema)
    problems += [f"{where}: unsupported keyword {k}" for k in sorted(keys & UNSUPPORTED)]
    problems += [f"{where}: unknown keyword {k}" for k in sorted(keys - SUPPORTED - UNSUPPORTED)]
    if "anyOf" in schema:
        for i, option in enumerate(schema["anyOf"]):
            problems += strict_problems(option, f"{where}.anyOf[{i}]")
        return problems
    kind = schema.get("type")
    if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        problems.append(f"{where}: type {kind!r}")
    if kind == "object":
        props = schema.get("properties")
        if not isinstance(props, dict):
            problems.append(f"{where}: an object needs properties, even empty")
            props = {}
        if schema.get("additionalProperties") is not False:
            problems.append(f"{where}: additionalProperties must be false")
        if schema.get("required") != list(props):
            problems.append(f"{where}: required must list every property, in order")
        for name, sub in props.items():
            problems += strict_problems(sub, f"{where}.{name}")
    if kind == "array":
        problems += strict_problems(schema.get("items"), f"{where}[]")
    if "enum" in schema and not all(isinstance(v, str) for v in schema["enum"]):
        problems.append(f"{where}: enum values must be strings")
    return problems


def test_every_tool_schema_follows_the_strict_rules() -> None:
    problems = []
    for tool in PLANNER_TOOLS:
        assert set(tool) == {"name", "description", "input_schema"}
        problems += strict_problems(tool["input_schema"], tool["name"])
    assert problems == []


def test_the_checker_rejects_what_strict_mode_rejects() -> None:
    optional = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    assert strict_problems(optional, "t") == [
        "t: additionalProperties must be false",
        "t: required must list every property, in order",
    ]
    assert strict_problems({"type": "string", "maxLength": 3}, "s") == [
        "s: unsupported keyword maxLength"
    ]
    empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    assert strict_problems(empty, "e") == []


def test_the_tools_are_the_read_brief_and_propose_tools_in_order() -> None:
    assert PLANNER_TOOL_NAMES == (
        "search_providers",
        "get_provider",
        "list_bookings",
        "update_brief",
        "ask_user",
        "propose_booking",
        "propose_cancel",
        "propose_reschedule",
        "remember",
        "list_memories",
        "propose_forget",
    )
    assert not set(PLANNER_TOOL_NAMES) & CONSEQUENTIAL_TOOLS


def test_what_reaches_the_api_is_strict_and_in_the_same_order() -> None:
    tracer = Tracer(run_id="r_t", thread_id="c_t", sinks=[NullSink()])
    gateway = LLMGateway(
        tracer=tracer,
        tools=PLANNER_TOOLS,
        settings=ModelSettings(model_mode="scripted"),
        transport=ScriptedChatModel(script=[]),
    )
    request = gateway.canonical([HumanMessage(content="hi")])
    assert [t["name"] for t in request["tools"]] == list(PLANNER_TOOL_NAMES)
    assert all(t["strict"] is True for t in request["tools"])
    assert request["tool_choice"] == {"type": "auto"}


def test_the_tool_list_fits_its_context_section() -> None:
    """S2 is 3,800 tokens with the 400-token tools preamble inside it (gate 2, 13.1).

    Rendered the way the gateway sends it (with `strict`), not as the Python literals.
    Measured in M8: 2,318 estimated tokens, 2,718 with the preamble. N1 adds
    `propose_cancel` and `propose_reschedule` (328 together) and `update_brief`'s
    `provider_pref` (98, with its evidence entry): 2,744, or 3,144 with the preamble, so
    656 were left for the three memory tools; N2B spent 699 of them (3,768 with the
    preamble, 32 left). `update_brief` is 1,379 of the list.
    """
    tracer = Tracer(run_id="r_t", thread_id="c_t", sinks=[NullSink()])
    gateway = LLMGateway(
        tracer=tracer,
        tools=PLANNER_TOOLS,
        settings=ModelSettings(model_mode="scripted"),
        transport=ScriptedChatModel(script=[]),
    )
    sent = gateway.canonical([HumanMessage(content="hi")])["tools"]
    size = est_tokens(json.dumps(sent, ensure_ascii=False, separators=(",", ":")))
    assert TOOLS_PREAMBLE_TOKENS + size <= CAPS["tools"] == 3_800
