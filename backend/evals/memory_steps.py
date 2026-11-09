# ruff: noqa: E501
"""Scenario verbs for memory and context (N2B): forget, chat actions, and the manifest.

The verbs are registered from `dsl.py` (one line for the models) and `runner.py` (one line
for the dispatch). Everything here goes through the real routes: a forget is the real
`DELETE /api/memory/{id}`, a chip click is the real `POST /actions`. The context checks
read the raw trace and the raw request, never `trace/reader.py` and never a product
helper, so the manifest cannot vouch for itself.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from evals.runner import Runner


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryRef(_Strict):
    """A memory row by what it is: key, scope and status (default: the active one)."""

    key: str | None = None
    scope: str = "global"
    status: str = "active"
    id: str | None = None  # a seed id such as m-12, when the key is not unique enough


class ForgetSpec(MemoryRef):
    expect_status: int = 200
    # repeat the same request with the same client_action_id and demand the same answer
    replay: bool = False
    # then a new client_action_id on the now-forgotten row: demand 409
    again: bool = False


class ForgetMemory(_Strict):
    forget_memory: ForgetSpec


class ActionSpec(_Strict):
    kind: Literal["keep_memory", "confirm_forget", "update_memory", "save_memory", "dismiss"]
    # which chip: the type of the latest chip of that kind in the conversation
    chip: Literal["memory_suggestion", "memory_forget", "memory_offer", "memory_update"]
    expect_status: int = 200
    choice: str | None = None
    again: bool = False  # click twice: the second must be 409


class MemoryAction(_Strict):
    memory_action: ActionSpec


class ChipSpec(_Strict):
    type: str
    count: int | None = None
    absent: bool = False
    # for a list card: the sizes of its two lists, written by hand in the scenario
    memory_ids: int | None = None
    suggestion_ids: int | None = None


class ExpectChip(_Strict):
    expect_chip: ChipSpec


class MemorySpec(MemoryRef):
    count: int | None = None
    kind: str | None = None
    evidence_type: str | None = None
    value: Any = None


class ExpectMemory(_Strict):
    expect_memory: MemorySpec


class ContextSpec(_Strict):
    """Checks on the raw `context.built` records and requests of a run (13.4)."""

    run: Literal["last", "all_since_forget"] = "last"
    call: int | None = None  # 1-based among the run's sent requests; default: the first
    included: list[str] | None = None  # exact set of memory ids in memory_included
    excluded: dict[str, str] | None = None  # every id here has this reason (a subset)
    prefill: dict[str, str] | None = None  # memory id -> the brief field it filled
    min_turns_dropped: int | None = None
    ledger_overflow: bool | None = None  # the request holds an "and N earlier errands" line
    s3_sub_capped: bool | None = None  # the request holds an "and N more upcoming bookings"
    min_request_tokens: int | None = None
    max_request_tokens: int | None = None
    stale: int | None = None  # guard.context_stale records
    rebuilt_calls: int | None = None  # context.built records with a memory_epoch change
    compacted: bool | None = None
    shed: list[str] | None = None
    absent: list[str] | None = None  # strings no request of the run (or since the forget) holds
    present: list[str] | None = None  # strings the first sent request holds
    max_over: int | None = None  # no request over this many estimated tokens
    min_masked: int | None = None  # n-gram mask hits in the first sent request


class ExpectContext(_Strict):
    expect_context: ContextSpec


MEMORY_STEP_TYPES: dict[str, type[BaseModel]] = {
    "forget_memory": ForgetMemory,
    "memory_action": MemoryAction,
    "expect_chip": ExpectChip,
    "expect_memory": ExpectMemory,
    "expect_context": ExpectContext,
}
MEMORY_STEPS = (ForgetMemory, MemoryAction, ExpectChip, ExpectMemory, ExpectContext)


class CallHook(_Strict):
    """Do something through the real routes just before the Nth model call of a run:
    the harness-side trigger of S49b, never product code."""

    before_call: int = Field(ge=1)
    forget: MemoryRef
    only_run: int | None = None  # 1-based among this scenario's runs; default: every run


Hook = Callable[[int], Awaitable[None]]


def describe(step: Any) -> str | None:
    if isinstance(step, ForgetMemory):
        return f"forget {step.forget_memory.key or step.forget_memory.id}"
    if isinstance(step, MemoryAction):
        return f"chip action {step.memory_action.kind}"
    if isinstance(step, ExpectChip):
        spec = step.expect_chip
        return f"no {spec.type} chip" if spec.absent else f"{spec.type} chip"
    if isinstance(step, ExpectMemory):
        mem = step.expect_memory
        return f"memory {mem.key or mem.id} {mem.status}"
    if isinstance(step, ExpectContext):
        return "context manifest"
    return None


# ------------------------------------------------------------------------ execution


def _rows(runner: Runner, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{runner.h.root / 'agent.sqlite'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def find_memory_id(root: Any, ref: MemoryRef) -> str:
    conn = sqlite3.connect(f"file:{root / 'agent.sqlite'}?mode=ro", uri=True)
    try:
        if ref.id:
            return ref.id
        found = conn.execute(
            "SELECT memory_id FROM memories WHERE key = ? AND scope = ? AND status = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (ref.key, ref.scope, ref.status),
        ).fetchall()
    finally:
        conn.close()
    if not found:
        raise AssertionError(f"no {ref.status} memory for {ref.key} in {ref.scope}")
    return str(found[0][0])


async def forget(runner: Runner, spec: ForgetSpec) -> None:
    memory_id = find_memory_id(runner.h.root, spec)
    action = runner.h.next_action_id()
    response = await runner.h.client.delete(f"/api/memory/{memory_id}?client_action_id={action}")
    runner.expect(
        response.status_code == spec.expect_status,
        f"forget {memory_id}: {response.status_code} {response.text[:200]}",
    )
    if spec.replay:
        again = await runner.h.client.delete(f"/api/memory/{memory_id}?client_action_id={action}")
        runner.expect(
            again.status_code == response.status_code and again.json() == response.json(),
            f"forget {memory_id}: a repeat with the same client_action_id answered {again.text[:200]}",
        )
    if spec.again:
        other = runner.h.next_action_id()
        second = await runner.h.client.delete(f"/api/memory/{memory_id}?client_action_id={other}")
        runner.expect(
            second.status_code == 409, f"forget {memory_id} twice: {second.status_code}, not 409"
        )
    runner.forgets.append(len(runner.runs))


async def _chips(runner: Runner, type_: str) -> list[dict[str, Any]]:
    convo = await runner.h.conversation()
    return [m for m in convo["messages"] if m["body"].get("type") == type_]


async def memory_action(runner: Runner, spec: ActionSpec) -> None:
    chips = await _chips(runner, spec.chip)
    if not chips:
        raise AssertionError(f"memory_action: no {spec.chip} chip in the conversation")
    chip = chips[-1]
    body = chip["body"]
    payload: dict[str, str] = {}
    if "memory_id" in body:
        payload["memory_id"] = body["memory_id"]
    if spec.kind in ("update_memory", "save_memory"):
        payload["chip_message_id"] = chip["message_id"]
    if spec.choice:
        payload["choice"] = spec.choice
    for i in range(2 if spec.again else 1):
        action = runner.h.next_action_id()
        if spec.kind == "dismiss":
            response = await runner.h.client.post(
                f"/api/memory/{payload['memory_id']}/dismiss", json={"client_action_id": action}
            )
        else:
            response = await runner.h.client.post(
                f"/api/conversations/{runner.h.conversation_id}/actions",
                json={"client_action_id": action, "kind": spec.kind, "payload": payload},
            )
        want = spec.expect_status if i == 0 else 409
        runner.expect(
            response.status_code == want,
            f"{spec.kind} (click {i + 1}): {response.status_code} {response.text[:200]}",
        )


async def expect_chip(runner: Runner, spec: ChipSpec) -> None:
    chips = await _chips(runner, spec.type)
    if spec.absent:
        runner.expect(not chips, f"expected no {spec.type} chip, found {len(chips)}")
        return
    if spec.count is not None:
        runner.expect(
            len(chips) == spec.count, f"{spec.type} chips: {len(chips)}, want {spec.count}"
        )
    elif not chips:
        runner.expect(False, f"no {spec.type} chip in the conversation")
        return
    if chips and spec.memory_ids is not None:
        got = len(chips[-1]["body"]["memory_ids"])
        runner.expect(
            got == spec.memory_ids, f"list card holds {got} memory ids, want {spec.memory_ids}"
        )
    if chips and spec.suggestion_ids is not None:
        got = len(chips[-1]["body"]["suggestion_ids"])
        runner.expect(
            got == spec.suggestion_ids,
            f"list card holds {got} suggestions, want {spec.suggestion_ids}",
        )


async def expect_memory(runner: Runner, spec: MemorySpec) -> None:
    clause, params = [], []
    if spec.id:
        clause.append("memory_id = ?")
        params.append(spec.id)
    if spec.key:
        clause += ["key = ?", "scope = ?"]
        params += [spec.key, spec.scope]
    clause.append("status = ?")
    params.append(spec.status)
    if spec.kind:
        clause.append("kind = ?")
        params.append(spec.kind)
    rows = _rows(
        runner,
        "SELECT memory_id, value_json, evidence_json FROM memories WHERE " + " AND ".join(clause),
        tuple(params),
    )
    want = 1 if spec.count is None else spec.count
    runner.expect(
        len(rows) == want, f"memories {spec.key or spec.id} {spec.status}: {len(rows)}, want {want}"
    )
    if spec.evidence_type and rows:
        evidence = json.loads(rows[0]["evidence_json"] or "{}")
        runner.expect(
            evidence.get("type") == spec.evidence_type,
            f"evidence type {evidence.get('type')!r}, want {spec.evidence_type!r}",
        )
    if spec.value is not None and rows:
        runner.expect(
            json.loads(rows[0]["value_json"]) == spec.value,
            f"value {rows[0]['value_json']}, want {spec.value!r}",
        )


def _request_text(record: dict[str, Any]) -> str:
    return json.dumps(record.get("request"), ensure_ascii=False)


async def expect_context(runner: Runner, spec: ContextSpec) -> None:
    rec = await runner.h.records()
    if spec.run == "all_since_forget":
        since = runner.forgets[-1] if runner.forgets else 0
        run_ids = [v.run_id for v in runner.runs[since:]]
    else:
        run_ids = [runner.last.run_id]
    records = [r for rid in run_ids for r in rec.traces.get(rid, [])]
    built = [r for r in records if r["type"] == "context.built" and r["manifest"].get("sent")]
    calls = [r for r in records if r["type"] == "model.call"]
    where = ",".join(run_ids)
    if not built:
        runner.expect(
            spec.included is None and spec.min_request_tokens is None, f"{where}: no sent request"
        )
    index = (spec.call or 1) - 1
    manifest = built[index]["manifest"] if len(built) > index else {}
    if spec.included is not None:
        got = sorted(i["id"] for i in manifest.get("memory_included", []))
        runner.expect(
            got == sorted(spec.included),
            f"{where}: memory_included {got}, want {sorted(spec.included)}",
        )
    if spec.excluded:
        have = manifest.get("memory_excluded", {})
        for memory_id, reason in spec.excluded.items():
            runner.expect(
                have.get(memory_id) == reason,
                f"{where}: {memory_id} is {have.get(memory_id)!r}, want {reason!r}",
            )
    if spec.prefill:
        have = {i["id"]: i.get("prefill") for i in manifest.get("memory_included", [])}
        for memory_id, name in spec.prefill.items():
            runner.expect(
                have.get(memory_id) == name,
                f"{where}: {memory_id} prefill {have.get(memory_id)!r}, want {name!r}",
            )
    if spec.min_turns_dropped is not None:
        got = manifest.get("turns_dropped", 0)
        runner.expect(got >= spec.min_turns_dropped, f"{where}: turns_dropped {got}")
    if spec.ledger_overflow is not None and calls:
        text = _request_text(calls[index] if len(calls) > index else calls[0])
        runner.expect(
            ("earlier errands" in text) == spec.ledger_overflow,
            f"{where}: ledger overflow line {not spec.ledger_overflow}",
        )
    if spec.s3_sub_capped is not None and calls:
        text = _request_text(calls[index] if len(calls) > index else calls[0])
        runner.expect(
            ("more upcoming bookings" in text) == spec.s3_sub_capped,
            f"{where}: S3 sub-cap line {not spec.s3_sub_capped}",
        )
    est = [c.get("est_request_tokens") or 0 for c in calls]
    if spec.min_request_tokens is not None:
        runner.expect(
            bool(est) and max(est) >= spec.min_request_tokens,
            f"{where}: largest request {max(est or [0])}",
        )
    if spec.max_request_tokens is not None or spec.max_over is not None:
        limit = spec.max_request_tokens or spec.max_over or 0
        runner.expect(
            all(e <= limit for e in est), f"{where}: a request over {limit}: {max(est or [0])}"
        )
    if spec.min_masked is not None:
        masked = manifest.get("masked", 0)
        runner.expect(
            masked >= spec.min_masked, f"{where}: masked {masked}, want at least {spec.min_masked}"
        )
    if spec.stale is not None:
        n_stale = sum(1 for r in records if r["type"] == "guard.context_stale")
        runner.expect(n_stale == spec.stale, f"{where}: {n_stale} stale calls, want {spec.stale}")
    if spec.rebuilt_calls is not None:
        epochs = [b["manifest"].get("memory_epoch") for b in built]
        n_rebuilt = sum(1 for a, b in itertools.pairwise(epochs) if a != b)
        runner.expect(
            n_rebuilt == spec.rebuilt_calls,
            f"{where}: {n_rebuilt} rebuilt requests, want {spec.rebuilt_calls}",
        )
    if spec.compacted is not None:
        did = any(b["manifest"].get("compacted") for b in built)
        runner.expect(did == spec.compacted, f"{where}: compacted {did}")
    if spec.shed is not None:
        got = [s for b in built for s in b["manifest"].get("shed", [])]
        runner.expect(sorted(set(got)) == sorted(spec.shed), f"{where}: shed {sorted(set(got))}")
    for needle in spec.absent or []:
        for call in calls:
            runner.expect(
                needle.casefold() not in _request_text(call).casefold(),
                f"{where}: {needle!r} reached request seq {call['seq']}",
            )
    for needle in spec.present or []:
        text = _request_text(calls[index]) if len(calls) > index else ""
        runner.expect(
            needle.casefold() in text.casefold(), f"{where}: {needle!r} not in the request"
        )


async def run(runner: Runner, step: Any) -> None:
    if isinstance(step, ForgetMemory):
        await forget(runner, step.forget_memory)
    elif isinstance(step, MemoryAction):
        await memory_action(runner, step.memory_action)
    elif isinstance(step, ExpectChip):
        await expect_chip(runner, step.expect_chip)
    elif isinstance(step, ExpectMemory):
        await expect_memory(runner, step.expect_memory)
    elif isinstance(step, ExpectContext):
        await expect_context(runner, step.expect_context)
    else:  # pragma: no cover
        raise AssertionError(f"no memory runner for {step!r}")
