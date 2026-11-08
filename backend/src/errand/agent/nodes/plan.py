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
- a world tool that is not a planner tool (book, ...) goes to the ToolGateway as the
  planner, which refuses it and records `guard.planner_tool_rejected`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from errand.agent import ask_guard, brief_ops, timewin
from errand.agent.deps import RunDeps
from errand.agent.planner_tools import (
    ASK_OPTION_MAX_CHARS,
    ASK_QUESTION_MAX_CHARS,
    PLANNER_TOOL_NAMES,
    PLANNER_TOOLS,
    AskUserArgs,
    GetProviderArgs,
    UpdateBriefArgs,
)
from errand.agent.shortlist import ranked_shortlist
from errand.agent.state import ThreadState
from errand.agent.templates import recommendation_line
from errand.agent.transitions import transition
from errand.context.builder import build_context
from errand.domain.brief import BriefField, TimeWindowExpr
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import UserMessage
from errand.domain.outcomes import (
    Ask,
    AskExhausted,
    Failure,
    Limit,
    LimitName,
    Options,
    PlanResult,
    Reply,
    TooLarge,
    ToolDown,
)
from errand.llm.errors import ContextOverBudget, ModelCallLimit
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
    GetProviderError,
    GetProviderOut,
    ListBookingsOut,
    SearchProvidersError,
    SearchProvidersOut,
)


@dataclass
class _End:
    """A tool call that ends the loop."""

    result: PlanResult


async def plan(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    assert state.errand is not None and isinstance(state.inbound, UserMessage)
    with deps.tracer.node("plan", phase_before=state.errand.phase.value) as span:
        loop = PlannerLoop(deps, state.errand, state.inbound)
        result = await loop.run()
        span.end_fields["phase_after"] = loop.errand.phase.value
    return {
        "errand": loop.errand,
        "plan_result": result,
        "counters": deps.counters.model_copy(deep=True),
    }


class PlannerLoop:
    def __init__(self, deps: RunDeps, errand: Errand, inbound: UserMessage) -> None:
        self.deps = deps
        self.errand = errand
        self.inbound = inbound
        self.searched = False
        self.booking_providers: set[str] = set()

    # ------------------------------------------------------------------ loop

    async def run(self) -> PlanResult:
        deps = self.deps
        async with deps.db.session() as repos:
            history = await repos.messages.list_for_conversation(deps.conversation_id)
        built = build_context(
            errand=self.errand,
            history=history,
            current=self.inbound,
            now=deps.clock.now(),
            tz=deps.tz,
            tools=PLANNER_TOOLS,
        )
        if built.over_cap is not None:
            over = built.over_cap
            deps.tracer.emit(
                "context.built",
                manifest={**built.manifest, "sent": False, "over_cap": over.section},
            )
            return TooLarge(section=over.section, estimated=over.estimated, cap=over.cap)
        session = deps.llm.session(
            system=built.system, messages=built.messages, manifest=built.manifest
        )
        for _ in range(deps.limits.loop_iterations):
            if self._past_deadline():
                return Limit(limit="deadline")
            try:
                turn = await session.call(on_token=deps.on_token)
            except ModelCallLimit:
                return Limit(limit="model_calls")
            except ContextOverBudget as exc:
                return TooLarge(section="request", estimated=exc.needed, cap=exc.budget)
            if turn.stop_reason == "end_turn":
                return self._finish(answer_text(turn.message))
            if turn.stop_reason != "tool_use":
                return Failure(reason=f"stop_reason {turn.stop_reason!r} is not handled until M13")
            calls = list(turn.message.tool_calls)
            if not calls:
                return Failure(reason="stop_reason tool_use without a tool call")
            replies: list[ToolMessage] = []
            for call in calls:
                handled = await self._handle(str(call["name"]), call.get("args") or {})
                if isinstance(handled, _End):
                    return handled.result
                content, is_error = handled
                replies.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=str(call["id"]),
                        status="error" if is_error else "success",
                    )
                )
            session.append(*replies)
        return Limit(limit="iterations")

    def _finish(self, text: str) -> PlanResult:
        shortlist = self.errand.shortlist
        fresh = shortlist is not None and shortlist.brief_version == self.errand.brief.version
        if self.searched and fresh and shortlist is not None and shortlist.candidates:
            return Options(recommendation=recommendation_line(text))
        # A search that found nobody has nothing to present: the model's answer goes out
        # as a reply until M8 replaces it with a code-written no-fit template.
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
                return self._update_brief(args)
            if name == "ask_user":
                return self._ask_user(args)
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
        shortlist = await ranked_shortlist(
            self.deps, result, window=window, budget=budget, brief_version=brief.version
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
        allowed = set(shortlist.provider_ids() if shortlist else []) | self.booking_providers
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
        return views.render(views.bookings_view(result, self.deps.tz)), False

    # ------------------------------------------------------------------ brief tools

    def _update_brief(self, args: Mapping[str, Any]) -> tuple[str, bool]:
        tracer = self.deps.tracer
        try:
            parsed = UpdateBriefArgs.model_validate(dict(args))
        except ValidationError as exc:
            problem = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            return views.render_error("invalid_arguments", detail=problem), True
        quotes: dict[str, str] = {}
        for item in parsed.evidence:
            quotes.setdefault(item.field, item.quote)
        sets: list[brief_ops.FieldValue] = []
        values = parsed.set.model_dump(exclude_none=True)
        for name in values:
            value: Any = getattr(parsed.set, name)
            if name == "window":
                value = TimeWindowExpr.model_validate(value.model_dump())
            sets.append(brief_ops.FieldValue(BriefField(name), value, quotes.get(name)))
        clears = [brief_ops.FieldValue(BriefField(n), None, quotes.get(n)) for n in parsed.clear]
        outcome = brief_ops.apply_patch(
            self.errand.brief,
            sets=sets,
            clears=clears,
            message_text=self.inbound.text,
            message_id=self.inbound.message_id,
            turn=self.inbound.turn_no,
            now=self.deps.clock.now(),
            tz=self.deps.tz,
        )
        set_names = [fv.name.value for fv in sets]
        clear_names = [fv.name.value for fv in clears]
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
        errand = self.errand.model_copy(update={"brief": outcome.brief})
        required_cleared = [
            f for f in outcome.cleared_fields if f in brief_ops.REQUIRED[errand.kind]
        ]
        if errand.phase is Phase.COMPARING and required_cleared:
            errand = transition(errand, Phase.GATHERING, "required_field_cleared", tracer)
        self.errand = errand
        gaps = brief_ops.missing(errand.brief, errand.kind)
        return views.render(
            {
                "ok": True,
                "known": brief_ops.known_lines(errand.brief),
                "missing": [f.value for f in gaps],
            }
        ), False

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
