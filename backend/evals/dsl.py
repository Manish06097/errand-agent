"""The scenario DSL (architecture 18.4): YAML in, strict Pydantic models out.

Every model forbids unknown keys, and a step must be exactly one known verb (plus its
modifiers), so a typo or a verb from a later milestone fails at load time instead of
silently doing nothing. M3 implements the verbs its scenarios need; later milestones
add theirs here, next to the runner code that executes them.

M4 verbs (the booking path): `until: approval_card`, `approve` (options `fingerprint:
stale`, `card: original`, and the answer it expects), `approve_concurrently: N`,
`decline`, `type_confirm` (options `composed_during_run`, `visible`, `ack`),
`click_slot`, `refresh_hold`, `advance`, `conversation` (switch to another chat of the
same user), `expect_card` and `expect_booking`. The harness reads cards the way the
client does: from `GET /api/conversations/{id}` and the run's SSE `card` events.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

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
    expect_notice: Literal[
        "retry",
        "narrow_search",
        "shorten",
        "answer",
        "refresh_hold",
        "raise_budget",
        "options",
        "check_now",
        "cancel",
    ]
    names: list[BriefField] = Field(default_factory=list)


class Until(_Strict):
    """Keep answering (questions from the goal, options by clicking the first slot) until
    an approval card is the last message, or fail after `max_turns`."""

    until: Literal["approval_card"]
    max_turns: int = Field(4, ge=1, le=8)


class ApproveSpec(_Strict):
    # The button press itself is expected to die with the API process (crash_at:
    # after_grant_commit): the harness answers 599 and the process exits with code 1.
    expect_crash: bool = False
    # stale: send the fingerprint of the card shown before the current one
    fingerprint: Literal["current", "stale"] = "current"
    # original: press the button on the first approval card of the conversation
    card: Literal["latest", "original"] = "latest"
    expect_status: int | None = None
    expect_error: str | None = None


class Approve(_Strict):
    approve: ApproveSpec = Field(default_factory=ApproveSpec)


class ApproveConcurrently(_Strict):
    """N button presses at once, each with its own client_action_id (a double click)."""

    approve_concurrently: int = Field(ge=2, le=5)


class DeclineSpec(_Strict):
    expect_status: int | None = None


class Decline(_Strict):
    decline: DeclineSpec = Field(default_factory=DeclineSpec)


class TypeConfirmSpec(_Strict):
    text: str = "book it"
    # The message was typed while the latest run was still running: its compose time is
    # the moment that run was started (the client queued it and sent it after the 409).
    composed_during_run: bool = False
    # Which card the client attaches as visible_card: the latest one it has, the
    # original action card of the live approval (not its reminder), the one it had on
    # screen when the latest run was started, or none at all.
    visible: Literal["latest", "original", "at_send", "none"] = "latest"
    # When the client acks the render of the latest card: as it arrived (the normal
    # client), only after the message was composed, or (a buggy client) stamped just
    # before the compose time whatever the truth.
    ack: Literal["on_render", "late", "early"] = "on_render"


class TypeConfirm(_Strict):
    type_confirm: TypeConfirmSpec = Field(default_factory=TypeConfirmSpec)


class ClickSlot(_Strict):
    click_slot: str = Field(pattern=r"^S\d+$")


class RefreshHold(_Strict):
    refresh_hold: dict[str, Any] = Field(default_factory=dict)


class Advance(_Strict):
    """Move the simulated clock (agent and world share it in process)."""

    advance: str


class Restart(_Strict):
    """Wait for the API to exit with code 1 (the scenario's `crash_at`), start it again on
    the same data directory without the fault and wait until it serves. The reconciler
    runs before the first request, so the recovery runs it started are in the transcript
    when this step returns; the runner then reads them like any other run."""

    restart: dict[str, Any] = Field(default_factory=dict)


class ReconcilerTick(_Strict):
    """Run the reconciler's re-check of `needs_attention` intents now (the dev drawer's
    "Reconcile now"): the evals set the periodic interval to 0 and drive every tick."""

    reconciler_tick: dict[str, Any] = Field(default_factory=dict)


class CheckNow(_Strict):
    """The "Check now" button on a PENDING_VERIFICATION message. `expect_status` 409
    asserts there was nothing to check."""

    check_now: dict[str, Any] = Field(default_factory=dict)


class CancelSpec(_Strict):
    # world cancel ops (every call, the 503 ones included)
    calls: int
    # every call carried the same idempotency key
    same_key: bool = True
    # the target booking's status in the world's own store afterwards
    booking_status: Literal["confirmed", "cancelled", "cancelled_by_provider"] | None = None


class ExpectCancel(_Strict):
    """What the world saw of the cancel, and where the booking ended up."""

    expect_cancel: CancelSpec


class MoveSpec(_Strict):
    # world reschedule ops (every call), and how many of them the world applied
    calls: int
    applied: int | None = None
    # the booking's ISO weekday in the world's own store afterwards (1 = Monday), or
    # `unchanged` for "still where the scenario seeded it"
    weekday: int | None = None
    unchanged: bool = False
    # every call carried the same idempotency key
    same_key: bool = True


class ExpectMove(_Strict):
    """What the world saw of the reschedule, and where the booking ended up."""

    expect_move: MoveSpec


class ClickAction(_Strict):
    """Press a button of the last run's notice, the way the web client does for a quick
    reply: its label is sent as the user's message. `kind` is the NoticeAction kind."""

    click_action: dict[str, str]


class SwitchConversation(_Strict):
    """Make `name` the current conversation, creating it on first use. "main" is the one
    every scenario starts in."""

    conversation: str = Field(min_length=1, max_length=40)


class CardSpec(_Strict):
    kind: Literal["action", "reminder", "outcome", "none"] | None = None
    status: str | None = None
    typed_grant_ok: bool | None = None
    price_delta_inr: int | None = None
    slot_hour: int | None = None
    # the card's slot starts at or after this local hour ("not before 10")
    slot_not_before_hour: int | None = None
    # text (case-insensitive) every one of which some line of the card must contain
    lines_include: list[str] = Field(default_factory=list)
    nearby: int | None = None
    # the price on the card is the held quote the world gave for its hold
    price_is_held_quote: bool | None = None


class ExpectCard(_Strict):
    """The last assistant message of the current conversation, as the client sees it."""

    expect_card: CardSpec


class TranscriptSpec(_Strict):
    # substrings (case-insensitive) no assistant message of the conversation may contain
    excludes: list[str] = Field(default_factory=list)
    # substrings (case-insensitive) the transcript must still contain (harmless text kept)
    includes: list[str] = Field(default_factory=list)
    # substrings the LAST assistant message's text must contain; the status line of that
    # message must contain every status_line_includes and none of status_line_excludes
    last_includes: list[str] = Field(default_factory=list)
    status_line_includes: list[str] = Field(default_factory=list)
    status_line_excludes: list[str] = Field(default_factory=list)


class ExpectTranscript(_Strict):
    """What the user can read: every assistant message of the current conversation, or its
    last message's text and status line."""

    expect_transcript: TranscriptSpec


class BookingSpec(_Strict):
    count: int
    slot_hour: int | None = None
    price_is_held_quote: bool | None = None


class ExpectBooking(_Strict):
    """Bookings the world created in this scenario (seeded history excluded)."""

    expect_booking: BookingSpec


class TwinSpec(_Strict):
    injection_reviews: Literal["clean"] = "clean"


class Twin(_Strict):
    """Replay the conversation so far in a fresh world where every injection review is
    a clean review with the same id and rating. The twin's search answers must be
    byte-identical to this run's, so ranks compare like with like."""

    twin: TwinSpec = Field(default_factory=TwinSpec)


class RankSpec(_Strict):
    provider: str
    no_better_than_twin: bool = True


class ExpectRank(_Strict):
    """The provider's place on the last shortlist is no better than in the twin's."""

    expect_rank: RankSpec


class ReaderSpec(_Strict):
    flagged: list[str] = Field(default_factory=list)
    not_flagged: list[str] = Field(default_factory=list)
    none_flagged: bool = False
    # Providers whose score_parts.review_signal must be 0; ["*"] means every candidate.
    no_review_bonus: list[str] = Field(default_factory=list)
    badge: dict[str, str] = Field(default_factory=dict)
    notice: list[str] = Field(default_factory=list)
    errors: dict[str, int] = Field(default_factory=dict)
    calls: int | None = None


class ExpectReader(_Strict):
    """What the quarantined reader did in the last run, and what reached the card."""

    expect_reader: ReaderSpec


class PlannerSpec(_Strict):
    # "tool" or "tool:provider_id"; each must appear among the last run's planner calls
    called: list[str] = Field(default_factory=list)
    # no propose_* naming these providers, in any run
    no_proposal_for: list[str] = Field(default_factory=list)


class ExpectPlanner(_Strict):
    """What the planner asked for (from its model.call responses), whatever code did."""

    expect_planner: PlannerSpec


class ExpectNoSideEffects(_Strict):
    """No hold, book, cancel or reschedule reached the world; the user's bookings are the
    seeded ones; no approval, intent or memory row exists. `pending_card_ok: true` (M4)
    allows what a legitimate proposal leaves: one hold and one pending approval, but still
    no book, cancel, reschedule, intent or memory."""

    expect_no_side_effects: dict[str, Any] = Field(default_factory=dict)


class BriefSpec(_Strict):
    version: int | None = None
    values: dict[BriefField, Any] = Field(default_factory=dict)
    # M8: provenance. The exact evidence span each fact carries, and the turn it was set
    # at; a fact a later message did not touch keeps both.
    evidence: dict[BriefField, str] = Field(default_factory=dict)
    set_at_turn: dict[BriefField, int] = Field(default_factory=dict)


class ExpectBrief(_Strict):
    expect_brief: BriefSpec


class BookCallsSpec(_Strict):
    # `book` calls the agent made (tool.call records), `book` rows in the world's op_log
    # (a duplicate delivery adds a row without a second agent call), distinct keys and
    # holds among them, and the op_log outcome of each row in order.
    agent_calls: int | None = None
    world_rows: int | None = None
    keys: int | None = None
    holds: int | None = None
    outcomes: list[str] | None = None


class ExpectBookCalls(_Strict):
    """M5: the shape of the book traffic, from the trace and the world's op_log."""

    expect_book_calls: BookCallsSpec


class RowsSpec(_Strict):
    table: Literal[
        "action_intents", "intent_attempts", "followup_jobs", "known_bookings", "approvals"
    ]
    where: dict[str, str | int] = Field(default_factory=dict)
    count: int


class ExpectRows(_Strict):
    """M5: rows in agent.sqlite (an intent's state, an attempt's calls, a follow-up job)."""

    expect_rows: list[RowsSpec]


Dedupe = Literal["on", "off"]


def _yaml_bool_modes(value: Any) -> Any:
    """YAML 1.1 reads a bare `on` or `off` as a boolean; the scenarios write them bare."""
    if isinstance(value, list):
        return ["on" if v is True else "off" if v is False else v for v in value]
    if isinstance(value, dict):
        return {("on" if k is True else "off" if k is False else k): v for k, v in value.items()}
    return value


class IfDedupe(_Strict):
    """M5: steps that apply in one dedupe mode only, where the facts differ between the
    two (`if_dedupe: {on: [...], off: [...]}`). A mode with no list runs nothing."""

    if_dedupe: Annotated[
        dict[Dedupe, list[Annotated[Any, BeforeValidator(lambda raw: parse_step(raw))]]],
        BeforeValidator(_yaml_bool_modes),
    ]


class PatchSpec(_Strict):
    set: list[BriefField] = Field(default_factory=list)
    clear: list[BriefField] = Field(default_factory=list)


class ExpectPatch(_Strict):
    """The fields the last run's applied `brief.patch` records set and cleared (M8)."""

    expect_patch: PatchSpec


class ShortlistSpec(_Strict):
    # substring of the shortlist card's window label
    window_label_includes: str | None = None
    # every slot on the card starts on one of these ISO weekdays (1 = Monday)
    slot_weekdays: list[int] | None = None
    # every slot on the card lies inside these local hours, ["07:00", "12:00"] (the
    # carried-over day part of a correction, M8 review)
    slot_hours: list[str] | None = None
    # the brief chips on the card name exactly these fields
    chips: list[BriefField] | None = None
    # every candidate carries its score parts
    score_parts: bool | None = None
    # the exclusion reasons on the card, as a set
    excluded_reasons: list[str] | None = None


class ExpectShortlist(_Strict):
    """The shortlist card the last run showed (M8)."""

    expect_shortlist: ShortlistSpec


class ExpectOptions(_Strict):
    """The quick replies of the last ClarifyCard, exactly (M8)."""

    expect_options: list[str]


class ExpectApprovals(_Strict):
    """Every approval row of the conversation as `status` or `status:reason`, in order."""

    expect_approvals: list[str]


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
    "until": Until,
    "approve": Approve,
    "approve_concurrently": ApproveConcurrently,
    "decline": Decline,
    "type_confirm": TypeConfirm,
    "click_slot": ClickSlot,
    "refresh_hold": RefreshHold,
    "advance": Advance,
    "conversation": SwitchConversation,
    "click_action": ClickAction,
    "restart": Restart,
    "reconciler_tick": ReconcilerTick,
    "check_now": CheckNow,
    "expect_cancel": ExpectCancel,
    "expect_move": ExpectMove,
    "expect_card": ExpectCard,
    "expect_transcript": ExpectTranscript,
    "expect_booking": ExpectBooking,
    "twin": Twin,
    "expect_rank": ExpectRank,
    "expect_reader": ExpectReader,
    "expect_planner": ExpectPlanner,
    "expect_no_side_effects": ExpectNoSideEffects,
    "expect_brief": ExpectBrief,
    "if_dedupe": IfDedupe,
    "expect_book_calls": ExpectBookCalls,
    "expect_rows": ExpectRows,
    "expect_patch": ExpectPatch,
    "expect_shortlist": ExpectShortlist,
    "expect_options": ExpectOptions,
    "expect_approvals": ExpectApprovals,
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
    | Until
    | Approve
    | ApproveConcurrently
    | Decline
    | TypeConfirm
    | ClickSlot
    | RefreshHold
    | Advance
    | SwitchConversation
    | ClickAction
    | Restart
    | ReconcilerTick
    | CheckNow
    | ExpectCancel
    | ExpectMove
    | ExpectCard
    | ExpectTranscript
    | ExpectBooking
    | Twin
    | ExpectRank
    | ExpectReader
    | ExpectPlanner
    | ExpectNoSideEffects
    | ExpectBrief
    | IfDedupe
    | ExpectBookCalls
    | ExpectRows
    | ExpectPatch
    | ExpectShortlist
    | ExpectOptions
    | ExpectApprovals
)


# A step that grants an approval can reach `book`; a scenario with one runs in both
# dedupe modes unless it says otherwise.
GRANTING = ("approve", "approve_concurrently", "type_confirm")


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


CrashPoint = Literal["after_intent_write", "after_world_book", "after_grant_commit"]
PlannerName = Literal[
    "scripted:reference",
    "scripted:injection_follower",
    "scripted:budget_poisoner",
    "hostile:direct_book",
    "hostile:false_claim",
    "hostile:paraphrase_claim",
    "hostile:cross_service_cite",
    "hostile:matching_quote",
    "hostile:no_evidence",
    "hostile:no_day_part",
    "hostile:reask",
]
ReaderName = Literal["keyword", "blind", "hostile", "skipper", "timeout", "refusal", "max_tokens"]


class ReplaySpec(_Strict):
    """`replay: {cassette: S01}` asks the collector for a second case with the same steps
    and expectations, served from `evals/cassettes/<cassette>.jsonl` (18.2, 18.6)."""

    cassette: str = Field(pattern=r"^[A-Za-z0-9_]+$")


class Scenario(_Strict):
    id: str
    title: str
    category: list[str]
    seed: int = 0
    model: PlannerName = "scripted:reference"
    # The scripted quarantined reader (evals/policies/reader.py).
    reader: ReaderName = "keyword"
    # Run against real processes and kill the API at this fault point on its first start
    # (ERRAND_FAULT); the steps then use `restart` (architecture 18.1, N1).
    crash_at: CrashPoint | None = None
    world: WorldConfig = Field(default_factory=WorldConfig)
    faults: FaultsConfig = Field(default_factory=FaultsConfig)
    user: UserSpec
    steps: list[Annotated[Step, BeforeValidator(parse_step)]]
    expect: FinalExpect = Field(default_factory=FinalExpect)
    replay: ReplaySpec | None = None
    # M5: the world's dedupe modes this scenario runs in. Default: both for a scenario
    # that can reach `book`, else just the one `world.enforce_idempotency` says. A single
    # mode that is not forced by `world.enforce_idempotency` needs a reason (one line).
    dedupe: Annotated[list[Dedupe] | None, BeforeValidator(_yaml_bool_modes)] = None
    dedupe_reason: str | None = None

    @model_validator(mode="after")
    def _dedupe_is_consistent(self) -> Scenario:
        if self.dedupe is None:
            return self
        if len(set(self.dedupe)) != len(self.dedupe) or not self.dedupe:
            raise ValueError("dedupe lists each mode once")
        if len(self.dedupe) == 1 and not self.dedupe_reason:
            raise ValueError(f"dedupe: {self.dedupe} runs one mode only; say why in dedupe_reason")
        if len(self.dedupe) == 2 and self.dedupe_reason:
            raise ValueError("dedupe_reason explains a single mode; this scenario runs both")
        if "off" not in self.dedupe and not self.world.enforce_idempotency:
            raise ValueError("world.enforce_idempotency: false contradicts dedupe: [on]")
        return self

    @property
    def reaches_book(self) -> bool:
        def grants(steps: list[Any]) -> bool:
            for s in steps:
                if isinstance(s, IfDedupe):
                    if any(grants(branch) for branch in s.if_dedupe.values()):
                        return True
                elif any(type(s) is STEP_TYPES[v] for v in GRANTING):
                    return True
            return False

        return grants(self.steps)

    def modes(self) -> list[Dedupe]:
        """The dedupe modes to run, in order."""
        if self.dedupe is not None:
            return list(self.dedupe)
        if not self.world.enforce_idempotency:
            return ["off"]
        return ["on", "off"] if self.reaches_book else ["on"]

    def label(self, mode: Dedupe) -> str:
        """The row and test id: plain for a scenario that never reaches `book`."""
        if not self.reaches_book and self.dedupe is None:
            return self.id
        return f"{self.id}[dedupe={mode}]"

    def for_mode(self, mode: Dedupe) -> Scenario:
        world = self.world.model_copy(update={"enforce_idempotency": mode == "on"})
        return self.model_copy(update={"world": world})

    def world_config(self) -> ScenarioConfig:
        return ScenarioConfig(id=self.id, seed=self.seed, world=self.world, faults=self.faults)

    @property
    def model_mode(self) -> str:
        return f"{self.model} + reader:{self.reader}"

    def world_label(self) -> str:
        names = self.faults.fault_names()
        if not self.world.enforce_idempotency:
            names = [*names, "dedupe off"]
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
