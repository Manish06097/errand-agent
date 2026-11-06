"""AgentDB: the process's one connection to agent.sqlite, shared safely between tasks.

aiosqlite runs every statement on one thread, but a transaction is connection state:
if two tasks shared the connection freely, one task's statements could land inside the
other's BEGIN IMMEDIATE. Every use therefore holds an asyncio lock for its whole
duration, either for a few autocommit statements (`session`) or for one explicit
transaction (`transaction`). Keep both short; nothing slow (a model or world call)
may run while the lock is held.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from errand.store.db import open_agent_db, transaction_immediate
from errand.store.repos.conversations import ConversationsRepo
from errand.store.repos.leases import LeasesRepo
from errand.store.repos.messages import MessagesRepo
from errand.store.repos.runs import RunsRepo
from errand.store.repos.users import UsersRepo


@dataclass(frozen=True)
class Repos:
    conn: aiosqlite.Connection
    users: UsersRepo
    conversations: ConversationsRepo
    messages: MessagesRepo
    runs: RunsRepo
    leases: LeasesRepo


class AgentDB:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._repos = Repos(
            conn=conn,
            users=UsersRepo(conn),
            conversations=ConversationsRepo(conn),
            messages=MessagesRepo(conn),
            runs=RunsRepo(conn),
            leases=LeasesRepo(conn),
        )

    @classmethod
    async def open(cls, path: Path) -> AgentDB:
        return cls(await open_agent_db(path))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Repos]:
        async with self._lock:
            yield self._repos

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Repos]:
        async with self._lock, transaction_immediate(self._conn):
            yield self._repos

    async def close(self) -> None:
        async with self._lock:
            await self._conn.close()
