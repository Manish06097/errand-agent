"""Every mutation of a stored preference (gate 2 decisions 3 to 11).

Each public method is one `BEGIN IMMEDIATE` transaction that appends `memory_events` and
bumps `users.memory_epoch`, so a run in flight can see that its memory set changed. UI
mutations carry a `client_action_id` and are idempotent through `client_requests`: a
retry gets the first answer back, and nothing else changes. A mutation on a row that is
not in the state it needs raises `MemoryConflict` with the row's current view (HTTP 409).

Context building and the model gateway never import this module: they read through
`selection`, `mask`, `scrub` and `registry` (an import-linter contract keeps it so).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import tzinfo
from typing import Any

import aiosqlite

from errand.memory import inference, mask, redaction
from errand.memory.briefjson import clear_fields
from errand.memory.evidence import Checked, EvidenceRefused, ValueCheck, check_explicit
from errand.memory.evidence import value_in_quote as default_value_check
from errand.memory.hmac_key import hmac_hex
from errand.memory.registry import (
    KeySpec,
    RegistryError,
    conflicts,
    dump_value,
    parse_value,
    render_value,
    surface_forms,
)
from errand.store.agent_db import AgentDB, Repos
from errand.store.repos.memories import MemoriesRepo, MemoryRow, Redaction, dumps

Json = dict[str, Any]


class MemoryError_(Exception):
    """Base of the errors a caller maps to an HTTP status."""


class MemoryNotFound(MemoryError_):
    pass


class MemoryConflict(MemoryError_):
    """The row is not in the state the action needs; `current` is its view (HTTP 409)."""

    def __init__(self, message: str, current: Json) -> None:
        super().__init__(message)
        self.current = current


class MemoryRefused(MemoryError_):
    """A write the registry's rules refuse (a contradiction, no evidence, a bad value)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class RouteMismatch(MemoryError_):
    """A client_action_id already used for a different action."""


def value_hash(value: Any) -> str:
    return hashlib.sha256(dump_value(value).encode("utf-8")).hexdigest()


def view(row: MemoryRow, *, events: Sequence[Any] | None = None) -> Json:
    """A row as the API and the UI see it. A forgotten row has no value; label is its key."""
    out: Json = {
        "memory_id": row.memory_id,
        "key": row.key,
        "scope": row.scope,
        "value": row.value,
        "label": None,
        "kind": row.kind,
        "status": row.status,
        "source": _source_label(row),
        "evidence": _evidence_view(row),
        "supersedes": row.supersedes,
        "superseded_by": row.superseded_by,
        "created_at": row.created_at,
        "confirmed_at": row.confirmed_at,
        "forgotten_at": row.forgotten_at,
        "last_used_at": row.last_used_at,
    }
    if row.value is not None:
        try:
            out["label"] = render_value(row.key, row.value)
        except RegistryError:
            out["label"] = None
    if events is not None:
        out["events"] = [
            {
                "event_id": e.event_id,
                "type": e.type,
                "actor": e.actor,
                "at": e.at,
                "details": e.details,
            }
            for e in events
        ]
    return out


def _source_label(row: MemoryRow) -> str:
    if row.kind == "explicit":
        return "You told me"
    return "Inferred, confirmed by you" if row.status == "active" else "Inferred from your bookings"


def _evidence_view(row: MemoryRow) -> Json | None:
    ev = row.evidence
    if ev is None:
        return None
    kind = ev.get("type")
    if kind == "user_msg":
        return {
            "type": kind,
            "message_id": ev.get("message_id"),
            "quote": ev.get("quote"),
            "cue": ev.get("cue"),
        }
    if kind == "rule":
        return {"type": kind, "rule": ev.get("rule"), "booking_ids": ev.get("booking_ids")}
    return {"type": kind, "client_action_id": ev.get("client_action_id")}


class MemoryService:
    def __init__(
        self,
        db: AgentDB,
        now_ms: Callable[[], int],
        hmac_key: bytes,
        *,
        value_check: ValueCheck = default_value_check,
    ) -> None:
        self._db = db
        self._now = now_ms
        self._key = hmac_key
        self._value_check = value_check

    # -------------------------------------------------------------------- idempotency

    async def _idempotent(
        self, route: str, client_action_id: str, body: Callable[[Repos, int], Awaitable[Json]]
    ) -> Json:
        async with self._db.transaction() as repos:
            prior = await repos.client_requests.get(client_action_id)
            if prior is not None:
                if prior.route != route or prior.response is None:
                    raise RouteMismatch(f"{client_action_id} was used for {prior.route}")
                return prior.response
            now = self._now()
            response = await body(repos, now)
            await repos.client_requests.record(client_action_id, route, response, now)
            return response

    # ----------------------------------------------------------------- explicit writes

    async def remember_explicit(
        self,
        *,
        user_id: str,
        key: str,
        scope: str,
        value: Any,
        message_id: str,
        quote: str,
        run_id: str | None = None,
        actor: str = "model",
    ) -> Json:
        """`remember`: a standing preference stated in the user's message, checked by code.

        Raises `MemoryRefused` for a bad key, scope or value and for missing evidence; the
        caller turns that into a tool error (or, with no cue, a brief patch and a chip).
        """
        try:
            spec, scope, canonical = parse_value(key, scope, value)
        except RegistryError as exc:
            raise MemoryRefused("bad_value", str(exc)) from None
        async with self._db.transaction() as repos:
            message = await repos.messages.get(message_id)
            if message is None or message.role != "user":
                raise MemoryRefused("no_user_message", "evidence must come from a user message")
            text = message.body.get("text")
            if not isinstance(text, str):
                raise MemoryRefused("no_user_message", "that message has no text")
            owner = await _owner_of(repos, message.conversation_id)
            if owner != user_id:
                raise MemoryRefused("no_user_message", "that message is not this user's")
            try:
                checked: Checked = check_explicit(
                    message=text,
                    quote=quote,
                    key=key,
                    scope=scope,
                    value=canonical,
                    value_check=self._value_check,
                )
            except EvidenceRefused as exc:
                raise MemoryRefused(exc.reason, exc.detail) from None
            evidence: Json = {
                "type": "user_msg",
                "message_id": message_id,
                "start": checked.start,
                "end": checked.end,
                "quote": checked.quote,
                "cue": checked.cue,
            }
            return await self._write_explicit(
                repos, user_id, spec, scope, canonical, evidence, actor, run_id, self._now()
            )

    async def remember_from_ui(
        self,
        *,
        user_id: str,
        key: str,
        scope: str,
        value: Any,
        client_action_id: str,
        action: str = "remember",
    ) -> Json:
        """A click is the cue: the "Remember this for next time?" chip, or an add on /memory."""
        try:
            spec, scope, canonical = parse_value(key, scope, value)
        except RegistryError as exc:
            raise MemoryRefused("bad_value", str(exc)) from None

        async def body(repos: Repos, now: int) -> Json:
            evidence: Json = {
                "type": "ui_action",
                "client_action_id": client_action_id,
                "action": action,
            }
            return await self._write_explicit(
                repos, user_id, spec, scope, canonical, evidence, "user", None, now
            )

        return await self._idempotent(f"memory.{action}", client_action_id, body)

    async def correct(
        self, *, user_id: str, memory_id: str, value: Any, client_action_id: str
    ) -> Json:
        """Edit on /memory or the Update chip: a new version, never an overwrite."""

        async def body(repos: Repos, now: int) -> Json:
            old = await self._owned(repos, user_id, memory_id)
            if old.status != "active":
                raise MemoryConflict(f"{memory_id} is {old.status}, not active", view(old))
            try:
                spec, scope, canonical = parse_value(old.key, old.scope, value)
            except RegistryError as exc:
                raise MemoryRefused("bad_value", str(exc)) from None
            evidence: Json = {
                "type": "ui_action",
                "client_action_id": client_action_id,
                "action": "correct",
            }
            return await self._write_explicit(
                repos, user_id, spec, scope, canonical, evidence, "user", None, now
            )

        return await self._idempotent("memory.correct", client_action_id, body)

    async def _write_explicit(
        self,
        repos: Repos,
        user_id: str,
        spec: KeySpec,
        scope: str,
        value: Any,
        evidence: Json,
        actor: str,
        run_id: str | None,
        now: int,
    ) -> Json:
        mem = repos.memories
        digest = value_hash(value)
        await self._refuse_contradiction(mem, user_id, spec, scope, value)

        replaced: list[MemoryRow] = []
        current = await mem.find(user_id, spec.key, scope, ["active"])
        if current and current[0].value_hash == digest:
            return {"memory": view(current[0]), "unchanged": True, "replaced": []}
        replaced += current
        # preferring and avoiding the same provider in one scope: the newer write wins
        if spec.contradicts is not None and spec.key in ("preferred_provider", "avoid_provider"):
            for row in await mem.find(user_id, spec.contradicts, scope, ["active"]):
                if conflicts(spec.key, value, row.key, row.value):
                    replaced.append(row)

        new_id = await mem.next_id()
        new = MemoryRow(
            new_id,
            user_id,
            spec.key,
            scope,
            value,
            digest,
            "explicit",
            "active",
            evidence,
            replaced[0].memory_id if current else None,
            None,
            now,
            None,
            None,
            None,
        )
        for old in replaced:
            ok = await mem.transition(
                old.memory_id, from_statuses=["active"], to="superseded", superseded_by=new_id
            )
            if not ok:  # pragma: no cover - the transaction holds the write lock
                raise MemoryConflict(f"{old.memory_id} changed", view(old))
        await mem.insert(new, dump_value(value), dumps(evidence))
        await mem.add_event(
            new_id,
            "written" if not current else "corrected",
            actor,
            now,
            run_id=run_id,
            details={
                "evidence": evidence.get("type"),
                "supersedes": [r.memory_id for r in replaced],
            },
        )
        for old in replaced:
            await mem.add_event(
                old.memory_id,
                "superseded",
                actor,
                now,
                run_id=run_id,
                details={"superseded_by": new_id},
            )
            await self._hide_superseded_span(repos, old, evidence)
        await self._reject_pending(
            mem, user_id, spec, scope, value, by=new_id, actor=actor, now=now
        )
        await mem.bump_epoch(user_id)
        stored = await mem.get(new_id)
        assert stored is not None
        return {
            "memory": view(stored),
            "unchanged": False,
            "replaced": [view(r) for r in replaced],
        }

    async def _refuse_contradiction(
        self, mem: MemoriesRepo, user_id: str, spec: KeySpec, scope: str, value: Any
    ) -> None:
        if spec.key not in ("earliest_time", "latest_time"):
            return
        other_key = spec.contradicts
        assert other_key is not None
        for other in await mem.find(user_id, other_key, scope, ["active"]):
            if conflicts(spec.key, value, other_key, other.value):
                raise MemoryRefused(
                    "contradiction",
                    f"{spec.key} {value} would be at or after the saved {other_key} {other.value}"
                    f" for {scope}",
                )

    async def _reject_pending(
        self,
        mem: MemoriesRepo,
        user_id: str,
        spec: KeySpec,
        scope: str,
        value: Any,
        *,
        by: str,
        actor: str,
        now: int,
    ) -> None:
        keys = [spec.key] + ([spec.contradicts] if spec.contradicts else [])
        for key in keys:
            for row in await mem.find(user_id, key, scope, ["pending_confirmation"]):
                if key != spec.key and not conflicts(spec.key, value, key, row.value):
                    continue
                moved = await mem.transition(
                    row.memory_id, from_statuses=["pending_confirmation"], to="rejected"
                )
                if moved:
                    await mem.add_event(
                        row.memory_id,
                        "rejected",
                        actor,
                        now,
                        details={"reason": "superseded_by_explicit", "by": by},
                    )

    async def _hide_superseded_span(self, repos: Repos, old: MemoryRow, new_evidence: Json) -> None:
        """The old evidence span stays in the transcript but is hidden from the model."""
        ev = old.evidence
        if not ev or ev.get("type") != "user_msg":
            return
        if new_evidence.get("type") == "user_msg" and (
            new_evidence.get("message_id") == ev.get("message_id")
            and new_evidence.get("start") == ev.get("start")
        ):
            return
        await repos.memories.add_redaction(
            Redaction(ev["message_id"], ev["start"], ev["end"], old.memory_id, "superseded")
        )

    # --------------------------------------------------------------------- suggestions

    async def suggest(
        self,
        *,
        user_id: str,
        key: str,
        scope: str,
        value: Any,
        rule: str,
        booking_ids: Sequence[str],
        run_id: str | None = None,
    ) -> Json | None:
        """A rule-inferred row, pending until the user clicks Keep (decision 4).

        Returns None, with the reason in `declined`, when no suggestion should be made:
        an active row for the same (key, scope) or its pair, a blocklisted value, or the
        same suggestion already pending.
        """
        try:
            spec, scope, canonical = parse_value(key, scope, value)
        except RegistryError as exc:
            raise MemoryRefused("bad_value", str(exc)) from None
        if spec.inferred_by != rule:
            raise MemoryRefused("no_such_rule", f"{key} is not inferred by {rule}")
        digest = value_hash(canonical)
        async with self._db.transaction() as repos:
            mem = repos.memories
            now = self._now()
            keys = [spec.key] + ([spec.contradicts] if spec.contradicts else [])
            for k in keys:
                for active in await mem.find(user_id, k, scope, ["active"]):
                    if k == spec.key or conflicts(spec.key, canonical, k, active.value):
                        return {"declined": "active_row_exists", "memory": None}
            if await mem.is_blocked(user_id, spec.key, scope, digest):
                return {"declined": "blocklisted", "memory": None}
            for row in await mem.find(user_id, spec.key, scope, ["pending_confirmation"]):
                if row.value_hash == digest:
                    return {"declined": "already_pending", "memory": view(row)}
            memory_id = await mem.next_id()
            evidence: Json = {"type": "rule", "rule": rule, "booking_ids": sorted(booking_ids)}
            new = MemoryRow(
                memory_id, user_id, spec.key, scope, canonical, digest, "inferred",
                "pending_confirmation", evidence, None, None, now, None, None, None,
            )  # fmt: skip
            await mem.insert(new, dump_value(canonical), dumps(evidence))
            await mem.add_event(
                memory_id, "suggested", f"rule:{rule}", now, run_id=run_id,
                details={"booking_ids": sorted(booking_ids)},
            )  # fmt: skip
            await mem.bump_epoch(user_id)
            return {"declined": None, "memory": view(new)}

    async def keep(self, *, user_id: str, memory_id: str, client_action_id: str) -> Json:
        async def body(repos: Repos, now: int) -> Json:
            row = await self._owned(repos, user_id, memory_id)
            mem = repos.memories
            moved = False
            if row.status == "pending_confirmation":
                try:
                    moved = await mem.transition(
                        memory_id,
                        from_statuses=["pending_confirmation"],
                        to="active",
                        confirmed_at=now,
                    )
                except aiosqlite.IntegrityError:
                    moved = False
            if not moved:
                current = await mem.get(memory_id)
                assert current is not None
                raise MemoryConflict(f"{memory_id} is {current.status}", view(current))
            await mem.add_event(memory_id, "confirmed", "user", now)
            await mem.bump_epoch(user_id)
            current = await mem.get(memory_id)
            assert current is not None
            return {"memory": view(current)}

        return await self._idempotent("memory.keep", client_action_id, body)

    async def dismiss(self, *, user_id: str, memory_id: str, client_action_id: str) -> Json:
        async def body(repos: Repos, now: int) -> Json:
            row = await self._owned(repos, user_id, memory_id)
            mem = repos.memories
            moved = await mem.transition(
                memory_id, from_statuses=["pending_confirmation"], to="rejected"
            )
            if not moved:
                raise MemoryConflict(f"{memory_id} is {row.status}", view(row))
            await mem.block(user_id, row.key, row.scope, row.value_hash, "dismissed")
            await mem.add_event(memory_id, "rejected", "user", now, details={"reason": "dismissed"})
            await mem.bump_epoch(user_id)
            current = await mem.get(memory_id)
            assert current is not None
            return {"memory": view(current)}

        return await self._idempotent("memory.dismiss", client_action_id, body)

    async def undo(self, *, user_id: str, memory_id: str, client_action_id: str) -> Json:
        """Undo on a "Saved" chip: not a standing preference. Nothing is redacted.

        If the save had replaced an older row, that row comes back: undoing a correction
        returns to what was saved before it.
        """

        async def body(repos: Repos, now: int) -> Json:
            row = await self._owned(repos, user_id, memory_id)
            mem = repos.memories
            if row.kind != "explicit" or not await mem.transition(
                memory_id, from_statuses=["active"], to="rejected"
            ):
                raise MemoryConflict(f"{memory_id} is {row.status}", view(row))
            await mem.add_event(memory_id, "rejected", "user", now, details={"reason": "undone"})
            restored: list[str] = []
            events = await mem.events(memory_id)
            for ev in events:
                for old_id in (ev.details or {}).get("supersedes", []):
                    old = await mem.get(old_id)
                    if old and old.status == "superseded" and old.superseded_by == memory_id:
                        await mem.reactivate(old_id)
                        await mem.add_event(
                            old_id, "restored", "user", now, details={"undone": memory_id}
                        )
                        restored.append(old_id)
            await mem.bump_epoch(user_id)
            current = await mem.get(memory_id)
            assert current is not None
            return {"memory": view(current), "restored": restored}

        return await self._idempotent("memory.undo", client_action_id, body)

    # ------------------------------------------------------------------------- forget

    async def forget(self, *, user_id: str, memory_id: str, client_action_id: str) -> Json:
        """Forget is final in agent.sqlite (decision 9): one transaction, value NULL last."""

        async def body(repos: Repos, now: int) -> Json:
            row = await self._owned(repos, user_id, memory_id)
            if row.status == "forgotten":
                raise MemoryConflict(f"{memory_id} is already forgotten", view(row))
            mem = repos.memories
            chain = [row]
            cursor = row
            while cursor.supersedes is not None:
                older = await mem.get(cursor.supersedes)
                if older is None or older.status == "forgotten":
                    break
                chain.append(older)
                cursor = older
            ids = [r.memory_id for r in chain]
            forms: dict[str, tuple[str, ...]] = {}
            span_hashes: dict[str, list[str]] = {}
            for r in chain:
                forms[r.memory_id] = surface_forms(r.key, r.value) if r.value is not None else ()
                await self._forget_row(repos, r, now, actor="user")
            cleared = await self._redact_messages(repos, user_id, chain, forms, span_hashes, now)
            archived = await self._cascade_archives(repos, user_id, set(ids), forms)
            for r in chain:
                await mem.add_hashes(
                    r.memory_id,
                    "surface_form",
                    sorted({h for f in forms[r.memory_id] if (h := self._hash(f))}),
                )
                await mem.add_hashes(
                    r.memory_id, "redacted_span", sorted(set(span_hashes.get(r.memory_id, [])))
                )
                await mem.blank_client_responses(r.memory_id)
            await mem.bump_epoch(user_id)
            for r in chain:  # last: the value and the quote leave the row
                await mem.null_value(r.memory_id, _bare_evidence(r.evidence))
            for r in chain:
                await mem.add_event(
                    r.memory_id,
                    "forgotten",
                    "user",
                    now,
                    details={
                        "with_earlier_values": len(chain) - 1,
                        "messages_redacted": cleared,
                        "briefs_cleared": archived,
                    },
                )
            return {
                "forgotten": ids,
                "earlier_values": len(chain) - 1,
                "messages_redacted": cleared,
                "briefs_cleared": archived,
                "memory": view((await mem.get(memory_id)) or row),
            }

        return await self._idempotent("memory.forget", client_action_id, body)

    async def _forget_row(self, repos: Repos, r: MemoryRow, now: int, *, actor: str) -> None:
        mem = repos.memories
        moved = await mem.transition(
            r.memory_id,
            from_statuses=["active", "pending_confirmation", "superseded", "rejected"],
            to="forgotten",
            forgotten_at=now,
        )
        if not moved:  # pragma: no cover - the transaction holds the write lock
            raise MemoryConflict(f"{r.memory_id} changed", view(r))
        await mem.block(r.user_id, r.key, r.scope, r.value_hash, "forgotten")

    def _hash(self, text: str) -> str | None:
        return mask.hash_of(self._key, text)

    async def _redact_messages(
        self,
        repos: Repos,
        user_id: str,
        chain: Sequence[MemoryRow],
        forms: dict[str, tuple[str, ...]],
        span_hashes: dict[str, list[str]],
        now: int,
    ) -> int:
        """Overwrite every surface form (and each evidence span) in the user's messages."""
        mem = repos.memories
        all_forms = tuple(f for r in chain for f in forms[r.memory_id])
        owner_of_form = {f: r.memory_id for r in chain for f in forms[r.memory_id]}
        evidence_spans: dict[str, list[tuple[int, int, str]]] = {}
        for r in chain:
            ev = r.evidence
            if ev and ev.get("type") == "user_msg":
                evidence_spans.setdefault(ev["message_id"], []).append(
                    (ev["start"], ev["end"], r.memory_id)
                )
        touched = 0
        for message_id, _role, body_json, created_at in await mem.user_messages(user_id):
            if created_at > now:
                continue
            body: Json = json.loads(body_json)
            text = redaction.primary_text(body)
            spans_here = evidence_spans.get(message_id, [])
            extra = [
                (s, e)
                for s, e, _ in spans_here
                if text is not None and text[s:e].strip() and e <= len(text)
            ]
            new_body, edited = redaction.redact_body(body, all_forms, extra)
            if new_body == body:
                continue
            touched += 1
            if text is not None:
                for s, e, mid in spans_here:
                    if e <= len(text) and (h := self._hash(text[s:e])):
                        span_hashes.setdefault(mid, []).append(h)
                for s, e in redaction.find_spans(text, all_forms):
                    owner = _owner_for_span(text[s:e], owner_of_form, chain)
                    if h := self._hash(text[s:e]):
                        span_hashes.setdefault(owner, []).append(h)
            existing = await mem.redactions_for_message(message_id)
            kept: list[Redaction] = []
            for old in existing:
                moved = redaction.shift_after(
                    (old.start, old.end), edited, len(redaction.FORGOTTEN_MARKER)
                )
                if moved is not None:
                    kept.append(Redaction(message_id, moved[0], moved[1], old.memory_id, old.kind))
            primary_owner = chain[0].memory_id
            for s, e in redaction.new_spans(edited, redaction.FORGOTTEN_MARKER):
                kept.append(Redaction(message_id, s, e, primary_owner, "forgotten"))
            await mem.replace_redactions(message_id, kept)
            await mem.set_message_body(
                message_id, json.dumps(new_body, ensure_ascii=False, separators=(",", ":"))
            )
        return touched

    async def _cascade_archives(
        self, repos: Repos, user_id: str, ids: set[str], forms: dict[str, tuple[str, ...]]
    ) -> int:
        mem = repos.memories
        all_forms = tuple(f for fs in forms.values() for f in fs)
        changed = 0
        for kind, ident, raw in await mem.archived_briefs(user_id):
            data: Any = json.loads(raw)
            if not isinstance(data, dict):
                continue
            cleared = clear_fields(data, lambda memory_id: memory_id in ids)
            edited = _scrub_evidence(data, all_forms)
            if cleared or edited:
                await mem.set_archived_brief(
                    kind, ident, json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                )
                changed += 1
        return changed

    async def run_inference(
        self, *, user_id: str, tz: tzinfo, run_id: str | None = None
    ) -> list[Json]:
        """After a completed errand: the two rules over the user's booking records.

        Returns the suggestions that were written (declined ones are not listed).
        """
        async with self._db.session() as repos:
            bookings = await repos.known_bookings.for_user(user_id)
        written: list[Json] = []
        for cand in inference.candidates(bookings, tz):
            got = await self.suggest(
                user_id=user_id,
                key=cand.key,
                scope=cand.scope,
                value=cand.value,
                rule=cand.rule,
                booking_ids=cand.booking_ids,
                run_id=run_id,
            )
            if got is not None and got["declined"] is None:
                written.append(got["memory"])
        return written

    # ----------------------------------------------------------------------- run usage

    async def mark_used(self, user_id: str, memory_ids: Sequence[str]) -> None:
        """`last_used_at` moves once per run, at run.end, for rows that were in a request."""
        async with self._db.transaction() as repos:
            await repos.memories.touch_used(list(memory_ids), self._now())

    # --------------------------------------------------------------------------- reads

    async def epoch(self, user_id: str) -> int:
        async with self._db.session() as repos:
            return await repos.memories.epoch(user_id)

    async def list_grouped(self, user_id: str) -> Json:
        async with self._db.session() as repos:
            rows = await repos.memories.for_user(user_id)
        by = {
            "preferences": [r for r in rows if r.status == "active"],
            "suggestions": [r for r in rows if r.status == "pending_confirmation"],
            "history": [r for r in rows if r.status in ("superseded", "forgotten", "rejected")],
        }
        return {name: [view(r) for r in group] for name, group in by.items()}

    async def detail(self, user_id: str, memory_id: str) -> Json:
        async with self._db.session() as repos:
            row = await self._owned(repos, user_id, memory_id)
            return view(row, events=await repos.memories.events(memory_id))

    async def _owned(self, repos: Repos, user_id: str, memory_id: str) -> MemoryRow:
        row = await repos.memories.get(memory_id)
        if row is None or row.user_id != user_id:
            raise MemoryNotFound(memory_id)
        return row


# ----------------------------------------------------------------------------- helpers


async def _owner_of(repos: Repos, conversation_id: str) -> str | None:
    async with repos.conn.execute(
        "SELECT user_id FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ) as cur:
        row = await cur.fetchone()
    return None if row is None else str(row[0])


def _bare_evidence(evidence: Json | None) -> str | None:
    """What survives of the evidence once a row is forgotten: ids and offsets, no quote."""
    if evidence is None:
        return None
    kept = {k: v for k, v in evidence.items() if k != "quote"}
    return dumps(kept)


def _owner_for_span(text: str, owner_of_form: dict[str, str], chain: Sequence[MemoryRow]) -> str:
    folded = text.casefold()
    for form, owner in owner_of_form.items():
        if form.casefold() == folded:
            return owner
    return chain[0].memory_id


def _scrub_evidence(data: Json, forms: Sequence[str]) -> bool:
    """Blank surface forms in the remaining evidence quotes of a stored brief."""
    changed = False
    for fact in data.values():
        if not isinstance(fact, dict):
            continue
        prov = fact.get("prov")
        if not isinstance(prov, dict):
            continue
        text = prov.get("evidence")
        if isinstance(text, str):
            spans = redaction.find_spans(text, forms)
            if spans:
                prov["evidence"] = redaction.overwrite(text, spans, redaction.FORGOTTEN_MARKER)
                changed = True
    return changed


def hmac_of(key: bytes, text: str) -> str | None:
    form = mask.normal_form(text)
    return hmac_hex(key, form) if form else None
