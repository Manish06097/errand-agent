"""Helpers for the API tests: a scripted planner that can be held mid-run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk

from errand.api.runs import ModelFactory
from errand.llm.scripted import ScriptedChatModel
from errand.world.scenario import ScenarioConfig
from evals.policies import reference

OPENER = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def world(scenario_id: str = "api_test") -> ScenarioConfig:
    return ScenarioConfig(id=scenario_id, seed=7)


class Gate:
    """Holds every model call until opened, and says when one is waiting."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.waiting = asyncio.Event()

    def open(self) -> None:
        self.opened.set()


class GatedModel(ScriptedChatModel):
    gate: Any = None

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        assert isinstance(self.gate, Gate)
        self.gate.waiting.set()
        await self.gate.opened.wait()
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


def gated_factory(gate: Gate) -> ModelFactory:
    def make() -> GatedModel:
        return GatedModel(script=reference.policy, gate=gate)

    return make
