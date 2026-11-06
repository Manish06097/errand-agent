"""The scenario DSL (architecture 18.4): YAML in, strict Pydantic models out.

Every model forbids unknown keys, and a step must be exactly one known verb (plus its
modifiers), so a typo or a verb from a later milestone fails at load time instead of
silently doing nothing. M3 implements the verbs its scenarios need; later milestones
add theirs here, next to the runner code that executes them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from errand.domain.brief import BriefField
from errand.domain.outcomes import RunOutcome
from errand.world.scenario import FaultsConfig, ScenarioConfig, WorldConfig


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as strings, like the world's scenario loader."""


_Loader.yaml_implicit_resolvers = {
    key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Say(_Strict):
    say: str
    # The fields this line gives, as the simulated user knows it; asking for one of them
    # again fails the scenario.
    gives: list[BriefField] = Field(default_factory=list)
    client_msg_id: str | None = None


class Answer(_Strict):
    """The simulated user answers the last ClarifyCard, structurally, per its persona."""

    answer: dict[str, Any] = Field(default_factory=dict)


class PostSpec(_Strict):
    text: str
    client_msg_id: str
    times: int = Field(2, ge=2, le=5)
    concurrent: bool = False
    gives: list[BriefField] = Field(default_factory=list)


class Post(_Strict):
    """POST the same client_msg_id several times; every response must name one run."""

    post: PostSpec


class ExpectOutcome(_Strict):
    expect_outcome: RunOutcome


class ExpectQuestionFor(_Strict):
    expect_question_for: list[BriefField]
    with_options: bool | None = None


class ExpectNoCard(_Strict):
    expect_no_card: dict[str, Any] = Field(default_factory=dict)


class ExpectWorld(_Strict):
    """Cumulative op_log counts by tool."""

    expect_world: dict[str, int]


class ExpectTrace(_Strict):
    """Record counts by type in the last run's trace."""

    expect_trace: dict[str, int]


class ToolErrorSpec(_Strict):
    tool: str
    error: str
    count: int


class ExpectToolErrors(_Strict):
    expect_tool_errors: ToolErrorSpec


class ExpectSearchMatchesBrief(_Strict):
    expect_search_matches_brief: dict[str, Any] = Field(default_factory=dict)


class ExpectNotice(_Strict):
    expect_notice: Literal["retry", "narrow_search", "shorten", "answer"]
    names: list[BriefField] = Field(default_factory=list)


STEP_TYPES: dict[str, type[_Strict]] = {
    "say": Say,
    "answer": Answer,
    "post": Post,
    "expect_outcome": ExpectOutcome,
    "expect_question_for": ExpectQuestionFor,
    "expect_no_card": ExpectNoCard,
    "expect_world": ExpectWorld,
    "expect_trace": ExpectTrace,
    "expect_tool_errors": ExpectToolErrors,
    "expect_search_matches_brief": ExpectSearchMatchesBrief,
    "expect_notice": ExpectNotice,
}

Step = (
    Say
    | Answer
    | Post
    | ExpectOutcome
    | ExpectQuestionFor
    | ExpectNoCard
    | ExpectWorld
    | ExpectTrace
    | ExpectToolErrors
    | ExpectSearchMatchesBrief
    | ExpectNotice
)


def parse_step(raw: Any) -> Step:
    if isinstance(raw, BaseModel):
        return raw  # type: ignore[return-value]
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"a step is a mapping with one verb, got {raw!r}")
    verbs = [k for k in raw if k in STEP_TYPES]
    if len(verbs) != 1:
        unknown = [k for k in raw if k not in STEP_TYPES]
        raise ValueError(
            f"step {raw!r} must have exactly one verb from {sorted(STEP_TYPES)}; "
            f"unknown keys: {unknown}"
        )
    model = STEP_TYPES[verbs[0]].model_validate(raw)
    return model  # type: ignore[return-value]


Persona = Literal["cooperative", "terse", "evasive"]


class UserSpec(_Strict):
    persona: Persona
    goal: dict[BriefField, str] = Field(default_factory=dict)


class FinalExpect(_Strict):
    invariants: list[str] = Field(default_factory=lambda: ["default"])


class Scenario(_Strict):
    id: str
    title: str
    category: list[str]
    seed: int = 0
    model: Literal["scripted:reference"] = "scripted:reference"
    world: WorldConfig = Field(default_factory=WorldConfig)
    faults: FaultsConfig = Field(default_factory=FaultsConfig)
    user: UserSpec
    steps: list[Annotated[Step, BeforeValidator(parse_step)]]
    expect: FinalExpect = Field(default_factory=FinalExpect)

    def world_config(self) -> ScenarioConfig:
        return ScenarioConfig(id=self.id, seed=self.seed, world=self.world, faults=self.faults)

    @property
    def model_mode(self) -> str:
        return self.model

    def world_label(self) -> str:
        names = self.faults.fault_names()
        return ", ".join(names) if names else "default"


def load_scenario(path: Path) -> Scenario:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a scenario is a YAML mapping")
    scenario = Scenario.model_validate(data)
    if scenario.id != path.stem:
        raise ValueError(f"{path}: id {scenario.id!r} must match the file name")
    return scenario


class CatalogueError(Exception):
    """The scenarios directory cannot be trusted to say what the suite covers."""


def find_scenarios(directory: Path) -> list[Path]:
    """Every scenario file, sorted; anything that would silently not run is an error.

    pytest turns an empty parametrize list into one skipped test and exits 0, so an
    empty or misnamed catalogue would pass `make evals` (BUG-M3-1). A scenario is a
    `*.yaml` file directly in `directory`; any other entry there (a `.yml` file, a
    subdirectory) is refused rather than left out. Dotfiles (.DS_Store) are ignored.
    """
    if not directory.is_dir():
        raise CatalogueError(f"{directory} does not exist")
    entries = sorted(p for p in directory.iterdir() if not p.name.startswith("."))
    stray = [p.name for p in entries if not (p.is_file() and p.suffix == ".yaml")]
    if stray:
        raise CatalogueError(
            f"{directory} holds entries that are not scenarios and would never run: "
            + ", ".join(stray)
            + " (a scenario is a *.yaml file directly in scenarios/)"
        )
    if not entries:
        raise CatalogueError(f"{directory} has no *.yaml scenarios; make evals needs at least one")
    return entries
