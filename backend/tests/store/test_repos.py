"""Repositories M3 builds on: users, conversations, messages, runs."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from errand.store.db import open_agent_db, transaction_immediate
from errand.store.repos.conversations import ConversationsRepo
from errand.store.repos.messages import Message, MessagesRepo
from errand.store.repos.runs import RunsRepo, RunTotals
from errand.store.repos.users import User, UsersRepo


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await open_agent_db(tmp_path / "agent.sqlite")
    await UsersRepo(conn).upsert(User("u_demo", "Asia/Kolkata", "Demo"))
    await ConversationsRepo(conn).create(conversation_id="c_1", user_id="u_demo", now_ms=1_000)
    yield conn
    await conn.close()


def _msg(
    message_id: str, *, client_msg_id: str | None, created_at: int, role: str = "user"
) -> Message:
    return Message(
        message_id=message_id,
        conversation_id="c_1",
        client_msg_id=client_msg_id,
        role=role,  # type: ignore[arg-type]
        kind="text",
        body={"text": f"hello {message_id}"},
        run_id=None,
        turn_no=1,
        approval_id=None,
        typed_grant_ok=None,
        rendered_at=None,
        created_at=created_at,
    )


async def test_users_upsert_and_get(db: aiosqlite.Connection) -> None:
    users = UsersRepo(db)
    await users.upsert(User("u_demo", "Asia/Kolkata", "Demo User"))
    assert await users.get("u_demo") == User("u_demo", "Asia/Kolkata", "Demo User")
    assert await users.get("nobody") is None


async def test_conversations_belong_to_a_user_and_activity_never_goes_back(
    db: aiosqlite.Connection,
) -> None:
    convs = ConversationsRepo(db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await convs.create(conversation_id="c_x", user_id="ghost", now_ms=1)
    await convs.create(conversation_id="c_2", user_id="u_demo", now_ms=2_000, kind="followup")
    await convs.touch("c_1", 5_000)
    await convs.touch("c_1", 3_000)  # a late writer
    c1 = await convs.get("c_1")
    assert c1 is not None and c1.last_activity_at == 5_000 and c1.created_at == 1_000
    assert [c.conversation_id for c in await convs.list_for_user("u_demo")] == ["c_1", "c_2"]


async def test_a_retried_post_gets_the_original_message_back(db: aiosqlite.Connection) -> None:
    messages = MessagesRepo(db)
    first, created = await messages.insert(_msg("m_1", client_msg_id="cm_1", created_at=10))
    assert created
    again, created = await messages.insert(_msg("m_retry", client_msg_id="cm_1", created_at=11))
    assert not created
    assert again == first
    assert await messages.get("m_retry") is None
    # Messages without a client id (assistant replies) never collide.
    await messages.insert(_msg("m_2", client_msg_id=None, created_at=12, role="assistant"))
    await messages.insert(_msg("m_3", client_msg_id=None, created_at=12, role="assistant"))
    order = [m.message_id for m in await messages.list_for_conversation("c_1")]
    assert order == ["m_1", "m_2", "m_3"]
    latest = await messages.latest_assistant("c_1")
    assert latest is not None and latest.message_id == "m_3"


async def test_card_render_ack_is_first_write_wins(db: aiosqlite.Connection) -> None:
    messages = MessagesRepo(db)
    card = replace(
        _msg("m_card", client_msg_id=None, created_at=5, role="assistant"),
        kind="card",
        typed_grant_ok=True,
    )
    await messages.insert(card)
    assert await messages.mark_rendered("m_card", 100)
    assert not await messages.mark_rendered("m_card", 200)
    stored = await messages.get("m_card")
    assert stored is not None
    assert stored.rendered_at == 100 and stored.typed_grant_ok is True and stored.kind == "card"


async def test_messages_reject_unknown_roles_and_conversations(db: aiosqlite.Connection) -> None:
    messages = MessagesRepo(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await messages.insert(_msg("m_bad", client_msg_id=None, created_at=1, role="system"))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await messages.insert(
            replace(_msg("m_orphan", client_msg_id=None, created_at=1), conversation_id="c_none")
        )


async def test_run_lifecycle(db: aiosqlite.Connection) -> None:
    runs = RunsRepo(db)
    async with transaction_immediate(db):
        run = await runs.start(
            run_id="r_1",
            conversation_id="c_1",
            trigger="user_message",
            now_ms=1_000,
            trace_path="data/traces/c_1/r_1.jsonl",
        )
    assert run.status == "running" and run.totals == RunTotals(0, 0, 0, 0, 0.0)
    await runs.start(run_id="r_2", conversation_id="c_1", trigger="approval", now_ms=2_000)
    assert [r.run_id for r in await runs.list_running()] == ["r_1", "r_2"]

    totals = RunTotals(
        model_calls=2, tool_calls=3, tokens_in=3709, tokens_out=10, cost_usd=0.0098756
    )
    assert await runs.end("r_1", outcome="ANSWERED", totals=totals, now_ms=1_500)
    assert not await runs.end("r_1", outcome="AGAIN", totals=totals, now_ms=1_600)
    assert await runs.mark_recovered("r_2", 3_000)
    assert not await runs.mark_recovered("r_2", 3_001)

    ended = await runs.get("r_1")
    assert ended is not None
    assert (ended.status, ended.outcome, ended.totals, ended.ended_at) == (
        "ended",
        "ANSWERED",
        totals,
        1_500,
    )
    assert await runs.list_running() == []
    assert [r.status for r in await runs.list_for_conversation("c_1")] == ["ended", "recovered"]


async def test_list_with_previews_takes_each_preview_from_its_own_conversation(
    db: aiosqlite.Connection,
) -> None:
    await UsersRepo(db).upsert(User("u_other", "Asia/Kolkata", "Other"))
    convs, messages = ConversationsRepo(db), MessagesRepo(db)
    await convs.create(conversation_id="c_2", user_id="u_demo", now_ms=2_000)
    await convs.create(conversation_id="c_3", user_id="u_demo", now_ms=3_000)
    await convs.create(conversation_id="c_other", user_id="u_other", now_ms=4_000)

    def say(
        message_id: str, conversation_id: str, text: str, at: int, role: str = "user"
    ) -> Message:
        return replace(
            _msg(message_id, client_msg_id=message_id, created_at=at, role=role),
            conversation_id=conversation_id,
            body={"text": text},
        )

    await messages.insert(say("m_a2", "c_2", "second thing", 2_100))
    await messages.insert(say("m_a1", "c_2", "ignored: not the first", 2_200))
    await messages.insert(say("m_asst", "c_1", "assistant words", 1_100, role="assistant"))
    await messages.insert(say("m_o", "c_other", "someone else's secret", 4_100))
    await messages.insert(say("m_c1", "c_1", "first thing", 1_200))

    rows = await convs.list_with_previews("u_demo")
    assert [(c.conversation_id, preview) for c, preview in rows] == [
        ("c_3", None),
        ("c_2", "second thing"),
        ("c_1", "first thing"),
    ]
    assert [c.conversation_id for c, _ in await convs.list_with_previews("u_demo", limit=2)] == [
        "c_3",
        "c_2",
    ]
