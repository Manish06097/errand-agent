"""Planner node: the bounded read-only tool loop, inside one node (6.4, ADR 0012).

Never imports the executor (import-linter). Only the resulting `PlanResult`, the
updated errand and the run counters are written to state; the loop's messages and
their thinking blocks never are.

The loop, per iteration: check the deadline, build or extend the request, one model
call through the `LoopSession` (prefix-checked, append-only), then read the response
by block type, never by position (thinking comes first, and Opus 5.5 returns text
written between tool calls inside thinking). `end_turn` ends the loop with the answer
text; `tool_use` runs each call in order, and their results, rendered and sanitized
once, are appended together; any other stop reason is INTERNAL_ERROR until M13 maps
them. Caps for M3: 8 model calls per run (gateway), 6 iterations per plan, 16 world
calls per run (tool gateway) and the 60 s simulated deadline before every model and
tool call. Hitting one ends LIMIT_REACHED.

How the model's tool calls are handled:
- search_providers: any argument is a tool error and nothing is forwarded; service,
  area and date_range come from the resolved brief, and a missing required field is a
  tool error naming it, with no world call. The answer is ranked by code, with the
  top candidates' reviews read by the quarantined reader first (agent/shortlist.py);
- get_provider: only an id from the current shortlist (built for this brief version)
  or from the user's bookings; the reviews go to the quarantined reader, and the view
  carries only its digest;
- list_bookings: `user_id` is injected;
- update_brief: evidence-checked, all-or-nothing field merge (brief_ops); a rejected
  patch also writes `guard.evidence_rejected`;
- ask_user: the ask guard (ask_guard); an accepted ask ends the loop;
- propose_booking (M4): `agent/validation.py` against the current shortlist, a fresh
  code-issued `list_bookings` and the unresolved intents. A rejection is a tool error
  the model can correct; a valid proposal ends the loop (prepare_proposal validates it
  again). More than one propose_* in one response is refused as a whole: every one of
  them gets "propose one action at a time", because guessing which one was meant
  would be the model deciding, not code. Calls after a valid proposal in the same
  response are not executed and are recorded as such;
- a world tool that is not a planner tool (book, ...) goes to the ToolGateway as the
  planner, which refuses it and records `guard.planner_tool_rejected`.

The errand-state section also carries the record-backed lines the context builder
takes (the last verified booking, the live approval card, this run's SystemNotes). A
SystemNote replan is a new invocation of this node, rebuilt from state (spike C2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from errand.agent import ask_guard, brief_ops, invalidation, pinned, steering, timewin
from errand.agent.changes.validate import check_change_proposal
from errand.agent.deps import RunDeps
from errand.agent.memory_fill import apply_memory
from errand.agent.memory_prefill import apply_service_prefill
from errand.agent.memory_tools import MEMORY_TOOL_NAMES, MemoryTools
from errand.agent.planner_tools import (
    ASK_OPTION_MAX_CHARS,
    ASK_QUESTION_MAX_CHARS,
    PLANNER_TOOL_NAMES,
    PLANNER_TOOLS,
    PROPOSE_TOOLS,
    AskUserArgs,
    GetProviderArgs,
    NotCoveredArg,
    ProposeBookingArgs,
    ProposeCancelArgs,
    UpdateBriefArgs,
)
from errand.agent.records import open_bookings, state_extras
from errand.agent.shortlist import ranked_shortlist
from errand.agent.state import CardChange, SystemNote, ThreadState
from errand.agent.templates import recommendation_line, so_far
from errand.agent.templates_conversation import covered_labels, no_fit_result
from errand.agent.transitions import transition
from errand.agent.validation import Rejected, check_book_proposal
from errand.context.builder import BuiltContext
from errand.context.rebuild import (
    build_with,
    gather,
    request_estimate,
)
from errand.context.rebuild import compacted as compacted_context
from errand.domain import aliases
from errand.domain.booking_refs import BOOKING_REF, assign
from errand.domain.brief import AskRecord, BriefField, TimeWindowExpr
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UserMessage
from errand.domain.outcomes import (
    Ask,
    AskExhausted,
    Exhausted,
    Failure,
    Limit,
    LimitName,
    NoAction,
    Options,
    OutOfCoverage,
    PlanResult,
    Proposal,
    Reply,
    TooLarge,
    ToolDown,
)
from errand.domain.slots import Shortlist
from errand.llm.errors import ContextOverBudget, ContextStale, ModelCallLimit
from errand.tools import views
from errand.tools.errors import (
    PlannerToolRejected,
    ToolBudgetExceeded,
    ToolInvalid,
    ToolUnavailable,
    ToolUnexpected,
)
from errand.tools.gateway import Caller
from errand.tools.sanitize import clean_text
from errand.world.contracts import (
    TOOLS,
    BookingItem,
    GetProviderError,
    GetProviderOut,
    ListBookingsOut,
    SearchProvidersError,
    SearchProvidersOut,
)

REPROMPTS_PER_RUN = 1
REBUILD_RESERVE = 60  # estimate slack for a rebuilt request, in tokens


@dataclass
class _End:
    """A tool call that ends the loop."""

    result: PlanResult


async def plan(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    assert state.errand is not None
    inbound = state.inbound
    if not isinstance(inbound, UserMessage):
        # A replan after an approval run's slot_taken (9.4 step 5): there is no message
        # in this run, so the user's last one stands in as the request.
        inbound = await _last_user_message(deps)
    with deps.tracer.node("plan", phase_before=state.errand.phase.value) as span:
        loop = PlannerLoop(deps, state.errand, inbound, state.system_notes)
        result = await loop.run()
        span.end_fields["phase_after"] = loop.errand.phase.value
    return {
        "errand": loop.errand,
        "plan_result": result,
        # A replan starts from a code node's result (a Replan); what plan decides now is
        # the run's result, not that one (BUG-M5-1).
        "result": None,
        "counters": deps.counters.model_copy(deep=True),
        "card_changes": [*state.card_changes, *loop.card_changes],
    }


async def _last_user_message(deps: RunDeps) -> UserMessage:
    async with deps.db.session() as repos:
        history = await repos.messages.list_for_conversation(deps.conversation_id)
    last = next((m for m in reversed(history) if m.role == "user"), None)
    assert last is not None, "a replan needs a user message in the conversation"
    text = last.body.get("text", "") if isinstance(last.body, dict) else ""
    return UserMessage(
        message_id=last.message_id,
        client_msg_id=last.client_msg_id,
        text=str(text),
        turn_no=last.turn_no or 0,
    )


def route_after_plan(state: ThreadState) -> str:
    return "prepare_proposal" if isinstance(state.plan_result, Proposal) else "respond"


class PlannerLoop:
    def __init__(
        self,
        deps: RunDeps,
        errand: Errand,
        inbound: UserMessage,
        notes: list[SystemNote] | None = None,
    ) -> None:
        self.deps = deps
        self.errand = errand
        self.inbound = inbound
        self.notes = list(notes or [])
        self.searched = False
        self.booking_providers: set[str] = set()
        # B1, B2, ... of the list_bookings view this run issued (N1): the only way a
        # proposal names a booking, and valid for this run only.
        self.booking_refs: dict[str, BookingItem] = {}
        self.card_changes: list[CardChange] = []
        # (tool, rendered result) of this run, for a rebuild or a compaction.
        self.results: list[tuple[str, str]] = []
        self.memory_tools = MemoryTools(self)

    def reject(self, tool: str, reason: str, **detail: Any) -> None:
        self._reject(tool, reason, **detail)

    async def update_brief_quiet(self, patch: Mapping[str, Any]) -> bool:
        """The no-cue path of `remember`: apply the value to this errand through the normal
        update_brief checks. True if the patch was applied."""
        out = await self._update_brief(patch)
        return not (isinstance(out, tuple) and out[1])

    # ------------------------------------------------------------------ loop

    async def run(self) -> PlanResult:
        deps = self.deps
        async with deps.db.session() as repos:
            history = await repos.messages.list_for_conversation(deps.conversation_id)
        extras = await state_extras(deps, [n.text for n in self.notes])
        built = await self._build(history, extras)
        if built.over_cap is not None:
            over = built.over_cap
            deps.tracer.emit(
                "context.built",
                manifest={**built.manifest, "sent": False, "over_cap": over.section},
            )
            if over.section == "current":
                return TooLarge(section=over.section, estimated=over.estimated, cap=over.cap)
            return Exhausted(section=over.section, estimated=over.estimated, cap=over.cap)
        session = deps.llm.session(
            system=built.system, messages=built.messages, manifest=built.manifest
        )
        compacted = False
        iterations = 0
        probe = self._epoch_probe if deps.memory is not None else None
        while iterations < deps.limits.loop_iterations:
            if self._past_deadline():
                return Limit(limit="deadline")
            try:
                turn = await session.call(on_token=deps.on_token, epoch_probe=probe)
            except ModelCallLimit:
                return Limit(limit="model_calls")
            except ContextStale as stale:
                deps.tracer.emit(
                    "guard.context_stale", detail={"built": stale.built, "current": stale.current}
                )
                rebuilt = await self._rebuild(compact=False)
                if rebuilt is None:
                    return Exhausted(
                        section="request", estimated=0, cap=deps.limits.context_budget_tokens
                    )
                session = deps.llm.session(
                    system=rebuilt.system, messages=rebuilt.messages, manifest=rebuilt.manifest
                )
                continue
            except ContextOverBudget as exc:
                if not self.results and not compacted:
                    # the first request of the run is over the budget on its own
                    return TooLarge(section="request", estimated=exc.needed, cap=exc.budget)
                if compacted:
                    return Exhausted(section="request", estimated=exc.needed, cap=exc.budget)
                rebuilt = await self._rebuild(compact=True)
                if rebuilt is None:
                    return Exhausted(section="request", estimated=exc.needed, cap=exc.budget)
                compacted = True
                session = deps.llm.session(
                    system=rebuilt.system, messages=rebuilt.messages, manifest=rebuilt.manifest
                )
                continue
            iterations += 1
            if turn.stop_reason == "end_turn":
                text = answer_text(turn.message)
                if self._points_at_a_selection(text):
                    if deps.counters.reprompts >= REPROMPTS_PER_RUN:
                        return NoAction()
                    deps.counters.reprompts += 1
                    deps.tracer.emit(
                        "guard.reprompt", detail={"reason": "selection_without_proposal"}
                    )
                    session.append(
                        HumanMessage(content=[{"type": "text", "text": steering.REPROMPT_TEXT}])
                    )
                    continue
                return self._finish(text)
            if turn.stop_reason != "tool_use":
                return Failure(reason=f"stop_reason {turn.stop_reason!r} is not handled until M13")
            calls = list(turn.message.tool_calls)
            if not calls:
                return Failure(reason="stop_reason tool_use without a tool call")
            proposals = sum(1 for c in calls if str(c["name"]) in PROPOSE_TOOLS)
            replies: list[ToolMessage] = []
            for i, call in enumerate(calls):
                name = str(call["name"])
                if name in PROPOSE_TOOLS and proposals > 1:
                    self._reject(name, "more_than_one_proposal", count=proposals)
                    handled: tuple[str, bool] | _End = (
                        views.render_error(
                            "one_proposal_at_a_time", detail="propose one action at a time"
                        ),
                        True,
                    )
                else:
                    handled = await self._handle(name, call.get("args") or {})
                if isinstance(handled, _End):
                    if isinstance(handled.result, Proposal):
                        self._not_executed(calls[i + 1 :])
                    return handled.result
                content, is_error = handled
                self.results.append((name, content))
                replies.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=str(call["id"]),
                        status="error" if is_error else "success",
                    )
                )
            session.append(*replies)
        return Limit(limit="iterations")

    # ------------------------------------------------------------------ context

    async def _epoch_probe(self) -> int:
        assert self.deps.memory is not None
        return await self.deps.memory.epoch(self.deps.user_id)

    async def _build(
        self,
        history: list[Any],
        extras: Any,
        *,
        shed: tuple[str, ...] = (),
        tool_results: tuple[str, ...] = (),
        compacted: bool = False,
    ) -> BuiltContext:
        deps = self.deps
        gathered = await gather(
            deps.db,
            user_id=deps.user_id,
            errand=self.errand,
            history=history,
            now=deps.clock.now(),
            tz=deps.tz,
            mask_key=deps.memory.mask_key if deps.memory is not None else None,
        )
        return build_with(
            gathered,
            errand=self.errand,
            history=history,
            current=self.inbound,
            now=deps.clock.now(),
            tz=deps.tz,
            tools=PLANNER_TOOLS,
            extras=extras,
            shed=shed,
            tool_results=tool_results,
            compacted=compacted,
        )

    async def _rebuild(self, *, compact: bool) -> BuiltContext | None:
        """A fresh request from state: after a memory change (stale epoch) or because the
        next call would not fit (compaction). The result is a new session's first request;
        the old session is dropped, never edited (its prefix check would refuse)."""
        deps = self.deps
        # A field whose memory is gone is cleared before anything renders it (decision 10).
        self.errand = await apply_memory(deps, self.errand, fresh=False, turn=self.inbound.turn_no)
        async with deps.db.session() as repos:
            history = await repos.messages.list_for_conversation(deps.conversation_id)
        extras = await state_extras(deps, [n.text for n in self.notes])
        rendered = tuple(f"[{name}] {content}" for name, content in self.results)
        gathered = await gather(
            deps.db,
            user_id=deps.user_id,
            errand=self.errand,
            history=history,
            now=deps.clock.now(),
            tz=deps.tz,
            mask_key=deps.memory.mask_key if deps.memory is not None else None,
        )
        tools_json = json.dumps(
            deps.llm.canonical([HumanMessage(content="x")]).get("tools"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        budget = deps.limits.context_budget_tokens
        kwargs: dict[str, Any] = {
            "errand": self.errand,
            "history": history,
            "current": self.inbound,
            "now": deps.clock.now(),
            "tz": deps.tz,
            "tools": PLANNER_TOOLS,
            "extras": extras,
            "tool_results": rendered,
        }
        built: BuiltContext | None = None
        if not compact:
            built = build_with(gathered, **kwargs)
            if request_estimate(built, tools_json) > budget - REBUILD_RESERVE:
                built = None
        if built is None:
            built = compacted_context(
                gathered, tools_json=tools_json, budget=budget, reserve=REBUILD_RESERVE, **kwargs
            )
        if built is None or built.over_cap is not None:
            return None
        return built

    def _not_executed(self, calls: list[Any]) -> None:
        # The loop ends on a valid proposal, so nothing after it in the response runs
        # (and no tool_result is owed: this session makes no further call).
        for call in calls:
            self._reject(str(call["name"]), "not_executed_after_proposal")

    def _current_shortlist(self) -> Shortlist | None:
        shortlist = self.errand.shortlist
        fresh = shortlist is not None and shortlist.brief_version == self.errand.brief.version
        return shortlist if fresh else None

    def _points_at_a_selection(self, text: str) -> bool:
        """Prose that says "I'll book it" while a current shortlist could be booked from."""
        shortlist = self._current_shortlist()
        return bool(
            shortlist is not None and shortlist.candidates and steering.points_at_a_selection(text)
        )

    def _finish(self, text: str) -> PlanResult:
        shortlist = self._current_shortlist()
        if self.searched and shortlist is not None and shortlist.candidates:
            return Options(recommendation=recommendation_line(text))
        if self.searched and shortlist is not None:
            # A search that found nobody has nothing to present: code names what could
            # widen (10.2); the model's words are not used.
            return no_fit_result(self.errand, so_far(self.errand))
        return Reply(text=text)

    def _past_deadline(self) -> bool:
        left = self.deps.counters.time_left_s(self.deps.clock.now())
        return left is not None and left <= 0

    # ------------------------------------------------------------------ dispatch

    async def _handle(self, name: str, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        try:
            if name == "search_providers":
                return await self._search(args)
            if name == "get_provider":
                return await self._get_provider(args)
            if name == "list_bookings":
                return await self._list_bookings(args)
            if name == "update_brief":
                return await self._update_brief(args)
            if name == "ask_user":
                return self._ask_user(args)
            if name == "propose_booking":
                return await self._propose_booking(args)
            if name == "propose_cancel":
                return await self._propose_cancel(args)
            if name == "propose_reschedule":
                return await self._propose_reschedule(args)
            if name in MEMORY_TOOL_NAMES:
                return await self.memory_tools.handle(name, args)
            if name in TOOLS:
                # Not a planner tool: let the gateway refuse it, so its allowlist is the
                # control that is exercised, not just this branch.
                await self.deps.tools.call(Caller.PLANNER, name, args)
                raise AssertionError("the gateway let the planner call " + name)
            self._reject(name, "unknown_tool")
            return views.render_error("unknown_tool", tools=list(PLANNER_TOOL_NAMES)), True
        except PlannerToolRejected:
            return views.render_error(
                "not_allowed", detail=f"{name} is not one of your tools"
            ), True
        except ToolBudgetExceeded as exc:
            limit: LimitName = "deadline" if exc.limit == "deadline" else "tool_calls"
            return _End(Limit(limit=limit))
        except ToolUnavailable as exc:
            return _End(ToolDown(tool=exc.tool, error_class=exc.error_class))
        except ToolUnexpected as exc:
            return _End(ToolDown(tool=exc.tool, error_class="ToolUnexpected"))
        except ToolInvalid:
            return views.render_error("invalid_arguments"), True

    def _reject(self, tool: str, reason: str, **detail: Any) -> None:
        self.deps.tracer.emit(
            "guard.planner_tool_rejected", detail={"tool": tool, "reason": reason, **detail}
        )

    # ------------------------------------------------------------------ read tools

    async def _search(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        if args:
            # Never forwarded: the brief decides what is searched, not the model.
            self._reject("search_providers", "arguments_not_allowed", keys=sorted(args))
            return views.render_error(
                "no_arguments",
                detail="search_providers takes no arguments; it searches the current brief",
            ), True
        brief = self.errand.brief
        gaps = brief_ops.missing(brief, self.errand.kind)
        if gaps:
            return views.render_error("brief_incomplete", missing=[f.value for f in gaps]), True
        assert brief.service and brief.area and brief.window
        window = brief.window.value
        result = await self.deps.tools.call(
            Caller.PLANNER,
            "search_providers",
            {
                "service": brief.service.value,
                "area": brief.area.value,
                "date_range": timewin.date_range(window, self.deps.tz),
            },
        )
        if isinstance(result, SearchProvidersError):
            return views.render_error(result.error, reason=result.reason), True
        assert isinstance(result, SearchProvidersOut)
        budget = brief.budget_max_inr.value if brief.budget_max_inr else None
        # Filter, provisional score, reader enrichment of the top candidates, final score,
        # order, handles: all before anything is rendered for the loop (10.6).
        pref = brief.provider_pref.value if brief.provider_pref else None
        shortlist = await ranked_shortlist(
            self.deps,
            result,
            window=window,
            budget=budget,
            brief_version=brief.version,
            prefer=pref.prefer_id if pref else None,
            avoid=pref.avoid_ids if pref else (),
        )
        errand = self.errand.model_copy(update={"shortlist": shortlist})
        if errand.phase is Phase.GATHERING:
            errand = transition(errand, Phase.COMPARING, "shortlist_built", self.deps.tracer)
        self.errand = errand
        self.searched = True
        return views.render(views.search_view(shortlist, result, self.deps.tz)), False

    async def _get_provider(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        try:
            parsed = GetProviderArgs.model_validate(dict(args))
        except ValidationError:
            return views.render_error("invalid_arguments", expected={"provider_id": "string"}), True
        shortlist = self.errand.shortlist
        # Only the current shortlist counts: one built for an earlier brief version is
        # what the context builder labels stale, and the planner may not open from it.
        if shortlist is not None and shortlist.brief_version != self.errand.brief.version:
            shortlist = None
        pinned_id = brief_ops.pinned(self.errand.brief)
        allowed = set(shortlist.provider_ids() if shortlist else []) | self.booking_providers
        if pinned_id is not None:
            allowed.add(pinned_id)
        if parsed.provider_id not in allowed:
            self._reject("get_provider", "not_in_shortlist_or_bookings")
            return views.render_error(
                "unknown_provider",
                detail="open only a provider from the current shortlist or the user's bookings",
            ), True
        result = await self.deps.tools.call(
            Caller.PLANNER, "get_provider", {"provider_id": parsed.provider_id}
        )
        if isinstance(result, GetProviderError):
            return views.render_error(result.error), True
        assert isinstance(result, GetProviderOut)
        window = self.errand.brief.window.value if self.errand.brief.window else None
        # Raw reviews go to the quarantined reader and nowhere else; the view carries
        # only its enum digest (None when the read was refused or failed).
        read = await self.deps.reader.read(result)
        if pinned_id == result.provider_id:
            # The provider the user chose by history may never appear in a search; its slots
            # that fit the window get handles, so propose_booking can name one (7.3).
            shortlist = pinned.with_pinned(
                shortlist,
                result,
                brief_version=self.errand.brief.version,
                window=window,
                digest=read.digest,
                now=self.deps.clock.now(),
                tz=self.deps.tz,
            )
            self.errand = self.errand.model_copy(update={"shortlist": shortlist})
        view = views.provider_view(
            result, window=window, shortlist=shortlist, tz=self.deps.tz, digest=read.digest
        )
        return views.render(view.model_dump(mode="json")), False

    async def _list_bookings(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        if args:
            self._reject("list_bookings", "arguments_not_allowed", keys=sorted(args))
            return views.render_error(
                "no_arguments", detail="list_bookings takes no arguments"
            ), True
        result = await self.deps.tools.call(
            Caller.PLANNER, "list_bookings", {"user_id": self.deps.user_id}
        )
        if not isinstance(result, ListBookingsOut):
            return views.render_error("unavailable"), True
        self.booking_providers |= {b.provider_id for b in result.bookings}
        self.booking_refs = assign(result.bookings, views.BOOKINGS_MAX)
        return views.render(views.bookings_view(result, self.deps.tz)), False

    # ------------------------------------------------------------------ brief tools

    async def _update_brief(self, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        tracer = self.deps.tracer
        try:
            parsed = UpdateBriefArgs.model_validate(dict(args))
        except ValidationError as exc:
            problem = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            return views.render_error("invalid_arguments", detail=problem), True
        if parsed.not_covered is not None:
            refused = self._check_not_covered(parsed.not_covered)
            if refused is not None:
                return refused, True
        quotes: dict[str, list[str]] = {}
        for item in parsed.evidence:
            quotes.setdefault(item.field, []).append(item.quote)
        pin: brief_ops.PinOutcome | None = None
        if parsed.set.provider_pref is not None:
            pinned_or_error = await self._pin(parsed.set.provider_pref, quotes)
            if isinstance(pinned_or_error, str):
                return pinned_or_error, True
            pin = pinned_or_error
        sets: list[brief_ops.FieldValue] = []
        values = parsed.set.model_dump(exclude_none=True)
        values.pop("provider_pref", None)  # handled above, by code
        for name in values:
            value: Any = getattr(parsed.set, name)
            if name == "window":
                value = TimeWindowExpr.model_validate(value.model_dump())
            sets.append(_field_value(name, value, quotes))
        clears = [_field_value(n, None, quotes) for n in parsed.clear]
        if parsed.not_covered is not None and not sets and not clears:
            return self._out_of_coverage(parsed.not_covered)
        outcome = brief_ops.apply_patch(
            pin.brief if pin is not None else self.errand.brief,
            sets=sets,
            clears=clears,
            message_text=self.inbound.text,
            message_id=self.inbound.message_id,
            turn=self.inbound.turn_no,
            now=self.deps.clock.now(),
            tz=self.deps.tz,
            earlier_messages=await self._earlier_user_messages()
            if any(fv.name is BriefField.WINDOW for fv in sets)
            else (),
        )
        set_names = [fv.name.value for fv in sets] + (["provider_pref"] if pin else [])
        clear_names = [fv.name.value for fv in clears]
        if pin is not None and outcome.applied:
            outcome.set_fields = [*pin.set_fields, *outcome.set_fields]
        if not outcome.applied:
            tracer.emit(
                "brief.patch",
                fields={"set": set_names, "clear": clear_names},
                evidence_ok=False,
                what={"message_id": self.inbound.message_id, "applied": False},
                reason="; ".join(outcome.errors)[:500],
            )
            # The guard record S16 and the eval table look for: a value the user's
            # message does not support was refused, whatever the planner was told.
            tracer.emit(
                "guard.evidence_rejected",
                detail={
                    "tool": "update_brief",
                    "fields": sorted({*set_names, *clear_names}),
                    "message_id": self.inbound.message_id,
                    "problems": len(outcome.errors),
                },
            )
            return views.render_error(
                "patch_rejected",
                problems=outcome.errors,
                user_message=self.inbound.text,
                detail="nothing was changed; quote the user's words exactly",
            ), True
        if outcome.cleared_fields:
            seen = list(outcome.brief.user_cleared)
            seen += [f for f in outcome.cleared_fields if f not in seen]
            outcome.brief = outcome.brief.model_copy(update={"user_cleared": seen})
        if outcome.set_fields:
            outcome.brief, filled = await apply_service_prefill(
                self.deps,
                outcome.brief,
                errand_kind=self.errand.kind.value,
                set_fields=outcome.set_fields,
                turn=self.inbound.turn_no,
                now=self.deps.clock.now(),
                tz=self.deps.tz,
            )
            outcome.set_fields += [f for f in filled if f not in outcome.set_fields]
        tracer.emit(
            "brief.patch",
            fields={
                "set": [f.value for f in outcome.set_fields],
                "clear": [f.value for f in outcome.cleared_fields],
            },
            evidence_ok=True,
            what={
                "message_id": self.inbound.message_id,
                "applied": True,
                "brief_version": outcome.brief.version,
            },
        )
        before = self.errand
        errand = self.errand.model_copy(update={"brief": outcome.brief})
        applied = await invalidation.apply(self.deps, errand, before.brief, outcome)
        errand = applied.errand
        self.card_changes += applied.changes
        required_cleared = [
            f for f in outcome.cleared_fields if f in brief_ops.REQUIRED[errand.kind]
        ]
        if errand.phase is Phase.COMPARING and required_cleared:
            errand = transition(errand, Phase.GATHERING, "required_field_cleared", tracer)
        self.errand = errand
        if parsed.not_covered is not None:
            return self._out_of_coverage(parsed.not_covered)
        gaps = brief_ops.missing(errand.brief, errand.kind)
        return views.render(
            {
                "ok": True,
                "known": brief_ops.known_lines(errand.brief),
                "missing": [f.value for f in gaps],
            }
        ), False

    async def _earlier_user_messages(self) -> list[str]:
        """The user's messages of this errand before the current one (9.9): from the
        message of the run that created the errand (its id is `e_<run_id>`), so a message
        from an earlier errand in the same chat is never evidence for this one."""
        first_run = self.errand.errand_id.removeprefix("e_")
        async with self.deps.db.session() as repos:
            history = await repos.messages.list_for_conversation(self.deps.conversation_id)
        texts: list[str] = []
        started = False
        for m in history:
            if m.message_id == self.inbound.message_id:
                break
            if m.role != "user":
                continue
            started = started or m.run_id == first_run
            if started and isinstance(m.body, dict) and isinstance(m.body.get("text"), str):
                texts.append(m.body["text"])
        return texts

    def _check_not_covered(self, claim: NotCoveredArg) -> str | None:
        """A tool error if `not_covered` is not what it says, else None (7.3, coverage)."""
        said = brief_ops.normalise(claim.words)
        check = brief_ops.check_evidence(
            claim.words, self.inbound.text, BriefField(claim.field), None
        )
        if not check.ok:
            self.deps.tracer.emit(
                "guard.evidence_rejected",
                detail={
                    "tool": "update_brief",
                    "fields": ["not_covered"],
                    "message_id": self.inbound.message_id,
                    "problems": 1,
                },
            )
            return views.render_error(
                "not_covered_rejected",
                problems=[check.reason],
                user_message=self.inbound.text,
                detail="quote the words the user used for the place or service, exactly",
            )
        for field in ("area", "service"):
            value = aliases.covered_value(field, said)
            if value is not None:
                self._reject("update_brief", "not_covered_is_covered", field=field)
                return views.render_error(
                    "is_covered",
                    detail=f"{claim.words!r} is {value}, which we cover; set {field} instead",
                )
        return None

    def _out_of_coverage(self, claim: NotCoveredArg) -> tuple[str, bool] | _End:
        """End the loop with the covered values as quick replies. The ask is recorded like
        any other, so the ask guard and INV-13 count it; if asking would break their rules
        (the field is already filled, or its two asks are spent) it is a notice instead."""
        brief = self.errand.brief
        field = BriefField(claim.field)
        asked_so_far = ask_guard.ask_counts(brief)[field]
        askable = (
            field in brief_ops.missing(brief, self.errand.kind)
            and field not in brief.ask_exhausted
            and asked_so_far < ask_guard.MAX_ASKS_PER_FIELD
        )
        if askable:
            record = AskRecord(field=field, turn=self.inbound.turn_no, with_options=True)
            brief = brief.model_copy(update={"asks": [*brief.asks, record]})
            self.errand = self.errand.model_copy(update={"brief": brief})
        words = clean_text(claim.words, limit=80)
        return _End(
            OutOfCoverage(
                field=claim.field,
                words=words,
                covered=covered_labels(claim.field),
                asked=askable,
            )
        )

    def _ask_user(self, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        tracer = self.deps.tracer
        try:
            parsed = AskUserArgs.model_validate(dict(args))
        except ValidationError as exc:
            problem = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            return views.render_error("invalid_arguments", detail=problem), True
        # The question and options reach the user verbatim on the ClarifyCard, so they
        # get the same cleaning as world text: no invisible characters, no phone numbers.
        question = clean_text(parsed.question, limit=ASK_QUESTION_MAX_CHARS)
        cleaned = (clean_text(o, limit=ASK_OPTION_MAX_CHARS) for o in parsed.options or [])
        options = list(dict.fromkeys(o for o in cleaned if o))
        if not question:
            return views.render_error("invalid_arguments", detail="question is empty"), True
        decision = ask_guard.check_ask(
            self.errand.brief, self.errand.kind, parsed.fields, options, self.inbound.turn_no
        )
        fields = [f.value for f in parsed.fields]
        if isinstance(decision, ask_guard.Reject):
            tracer.emit("guard.ask_rejected", detail={"fields": fields, "reason": decision.reason})
            return views.render_error("ask_rejected", detail=decision.reason), True
        if isinstance(decision, ask_guard.Exhausted):
            tracer.emit(
                "guard.ask_rejected",
                detail={
                    "fields": fields,
                    "reason": "ask budget spent",
                    "spent": [f.value for f in decision.spent],
                    "still_needed": [f.value for f in decision.still_needed],
                },
            )
            self.errand = self.errand.model_copy(update={"brief": decision.brief})
            return _End(AskExhausted(still_needed=decision.still_needed))
        self.errand = self.errand.model_copy(update={"brief": decision.brief})
        return _End(Ask(fields=list(parsed.fields), question=question, options=options))

    # ------------------------------------------------------------------ proposals

    async def _propose_booking(self, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        try:
            parsed = ProposeBookingArgs.model_validate(dict(args))
        except ValidationError:
            return views.render_error(
                "invalid_arguments", expected={"provider_id": "string", "slot_ref": "S1"}
            ), True
        deps = self.deps
        listed = await deps.tools.call(Caller.PROPOSER, "list_bookings", {"user_id": deps.user_id})
        bookings: list[BookingItem] | None = (
            list(listed.bookings) if isinstance(listed, ListBookingsOut) else None
        )
        if bookings is None:
            return views.render_error("unavailable", detail="bookings could not be read"), True
        verdict = check_book_proposal(
            self.errand,
            parsed.provider_id,
            parsed.slot_ref,
            now=deps.clock.now(),
            tz=deps.tz,
            bookings=bookings,
            unresolved=await open_bookings(deps),
        )
        if isinstance(verdict, Rejected):
            self._reject("propose_booking", verdict.code)
            return views.render_error(verdict.code, detail=verdict.message), True
        return _End(
            Proposal(action="book", provider_id=parsed.provider_id, slot_ref=parsed.slot_ref)
        )

    async def _pin(self, ref: str, quotes: Mapping[str, list[str]]) -> brief_ops.PinOutcome | str:
        """`provider_pref` is a list_bookings handle; code resolves it to the provider, reads
        the provider's area from `get_provider`, and pins (7.3). A tool error (the string)
        if the handle is not from this run's view or the evidence is not the user's."""
        deps = self.deps
        booking = self.booking_refs.get(ref) if BOOKING_REF.fullmatch(ref) else None
        if booking is None:
            self._reject("update_brief", "unknown_booking_ref", field="provider_pref")
            return views.render_error(
                "unknown_booking_ref",
                detail=(
                    f"{ref!r} is not a handle from a list_bookings result in this turn; "
                    "call list_bookings first"
                ),
            )
        found = await deps.tools.call(
            Caller.PLANNER, "get_provider", {"provider_id": booking.provider_id}
        )
        if not isinstance(found, GetProviderOut):
            return views.render_error("unavailable", detail="the provider could not be read")
        spans = quotes.get("provider_pref") or []
        outcome = brief_ops.pin_from_history(
            self.errand.brief,
            booking=booking,
            provider_area=found.area,
            quote=spans[0] if spans else None,
            message_text=self.inbound.text,
            message_id=self.inbound.message_id,
            turn=self.inbound.turn_no,
        )
        if not outcome.applied:
            deps.tracer.emit(
                "brief.patch",
                fields={"set": ["provider_pref"], "clear": []},
                evidence_ok=False,
                what={"message_id": self.inbound.message_id, "applied": False},
                reason="; ".join(outcome.errors)[:500],
            )
            deps.tracer.emit(
                "guard.evidence_rejected",
                detail={
                    "tool": "update_brief",
                    "fields": ["provider_pref"],
                    "message_id": self.inbound.message_id,
                    "problems": len(outcome.errors),
                },
            )
            return views.render_error(
                "patch_rejected",
                problems=outcome.errors,
                user_message=self.inbound.text,
                detail="nothing was changed; quote the user's words exactly",
            )
        return outcome

    async def _change_target(
        self, tool: str, args: Mapping[str, Any]
    ) -> tuple[str, bool] | BookingItem:
        """The booking a propose_cancel or propose_reschedule names, checked: the handle is
        from this run's view, and the booking is the user's, confirmed, upcoming, with no
        change already working on it, in a fresh list (N1)."""
        try:
            parsed = ProposeCancelArgs.model_validate(dict(args))
        except ValidationError:
            return views.render_error("invalid_arguments", expected={"booking_ref": "B1"}), True
        deps = self.deps
        if not BOOKING_REF.fullmatch(parsed.booking_ref):
            self._reject(tool, "bad_booking_ref")
            return views.render_error(
                "bad_booking_ref", detail=f"{parsed.booking_ref!r} is not a handle such as B1"
            ), True
        booking = self.booking_refs.get(parsed.booking_ref)
        if booking is None:
            self._reject(tool, "unknown_booking_ref")
            return views.render_error(
                "unknown_booking_ref",
                detail=(
                    f"{parsed.booking_ref} is not in a list_bookings result from this turn; "
                    "call list_bookings first"
                ),
            ), True
        listed = await deps.tools.call(Caller.PROPOSER, "list_bookings", {"user_id": deps.user_id})
        if not isinstance(listed, ListBookingsOut):
            return views.render_error("unavailable", detail="bookings could not be read"), True
        async with deps.db.session() as repos:
            live = await repos.intents.live_targets(deps.user_id)
            waiting = await repos.intents.awaiting_provider(deps.conversation_id)
        verdict = check_change_proposal(
            self.errand,
            booking.booking_id,
            listed.bookings,
            now=deps.clock.now(),
            live_targets=live,
            awaiting_provider=waiting,
        )
        if isinstance(verdict, Rejected):
            self._reject(tool, verdict.code)
            return views.render_error(verdict.code, detail=verdict.message), True
        return verdict.booking

    async def _propose_cancel(self, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        target = await self._change_target("propose_cancel", args)
        if isinstance(target, tuple):
            return target
        return _End(Proposal(action="cancel", booking_id=target.booking_id))

    async def _propose_reschedule(self, args: Mapping[str, Any]) -> tuple[str, bool] | _End:
        target = await self._change_target("propose_reschedule", args)
        if isinstance(target, tuple):
            return target
        if self.errand.brief.window is None:
            self._reject("propose_reschedule", "no_window")
            return views.render_error(
                "no_window",
                detail=(
                    "the new day is not set; record the user's day with update_brief (window) "
                    "or ask the user which day"
                ),
            ), True
        return _End(Proposal(action="reschedule", booking_id=target.booking_id))


def _field_value(name: str, value: Any, quotes: Mapping[str, list[str]]) -> brief_ops.FieldValue:
    spans = quotes.get(name) or []
    return brief_ops.FieldValue(
        BriefField(name), value, spans[0] if spans else None, tuple(spans[1:])
    )


def answer_text(message: AIMessage) -> str:
    """The answer's text blocks, in order. Thinking and tool_use blocks are skipped."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts).strip()
