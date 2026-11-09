"""Fixtures for the memory service: a real agent.sqlite, one user, one chat."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from errand.memory import hmac_key
from errand.memory.service import MemoryService
from errand.store.agent_db import AgentDB
from errand.store.repos.conversations import ConversationsRepo
from errand.store.repos.messages import Message
from errand.store.repos.users import User

USER = "u_demo"
CHAT = "c_0001"


@dataclass
class Env:
    db: AgentDB
    service: MemoryService
    key: bytes
    clock: list[int] = field(default_factory=lambda: [1_000_000])
    _n: int = 0
    _a: int = 0

    def tick(self, ms: int = 1_000) -> int:
        self.clock[0] += ms
        return self.clock[0]

    def action(self) -> str:
        self._a += 1
        return f"act-{self._a}"

    async def say(
        self, text: str, *, role: str = "user", body: dict[str, Any] | None = None
    ) -> str:
        """Store a message in the chat and return its id."""
        self._n += 1
        message_id = f"msg_{self._n:04d}"
        async with self.db.session() as repos:
            await repos.messages.insert(
                Message(
                    message_id,
                    CHAT,
                    f"cm-{self._n}" if role == "user" else None,
                    role,  # type: ignore[arg-type]
                    "text",
                    body if body is not None else {"type": "text", "text": text},
                    None,
                    self._n,
                    None,
                    None,
                    None,
                    self.tick(),
                )
            )
        return message_id

    async def remember(self, key: str, scope: str, value: Any, text: str, quote: str | None = None):  # type: ignore[no-untyped-def]
        message_id = await self.say(text)
        return await self.service.remember_explicit(
            user_id=USER,
            key=key,
            scope=scope,
            value=value,
            message_id=message_id,
            quote=quote or text,
        )

    async def dump(self) -> str:
        async with self.db.session() as repos:
            return "\n".join([line async for line in repos.conn.iterdump()])

    async def row(self, memory_id: str) -> Any:
        async with self.db.session() as repos:
            return await repos.memories.get(memory_id)

    async def body(self, message_id: str) -> dict[str, Any]:
        async with self.db.session() as repos:
            m = await repos.messages.get(message_id)
        assert m is not None
        return m.body

    async def epoch(self) -> int:
        return await self.service.epoch(USER)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    db = await AgentDB.open(tmp_path / "agent.sqlite")
    async with db.session() as repos:
        await repos.users.upsert(User(USER, "Asia/Kolkata", "Demo"))
        await ConversationsRepo(repos.conn).create(conversation_id=CHAT, user_id=USER, now_ms=1_000)
    key = hmac_key.fixed_key("service-tests")
    holder: list[int] = [1_000_000]
    service = MemoryService(db, lambda: holder[0], key)
    yield Env(db, service, key, holder)
    await db.close()


def loads(raw: str) -> Any:
    return json.loads(raw)
