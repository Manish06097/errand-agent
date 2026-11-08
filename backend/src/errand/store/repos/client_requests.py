"""client_requests: idempotent UI POSTs keyed by `client_action_id` (ADR 0011).

The first request with an id stores its answer; a retry with the same id gets that
answer back and starts nothing. An id reused on a different route is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True)
class ClientRequest:
    client_action_id: str
    route: str
    response: dict[str, Any] | None
    created_at: int


class ClientRequestsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self, client_action_id: str) -> ClientRequest | None:
        async with self._conn.execute(
            "SELECT client_action_id, route, response_json, created_at FROM client_requests "
            "WHERE client_action_id = ?",
            (client_action_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        body = json.loads(row[2]) if row[2] else None
        return ClientRequest(row[0], row[1], body, row[3])

    async def record(
        self, client_action_id: str, route: str, response: dict[str, Any], now_ms: int
    ) -> bool:
        cur = await self._conn.execute(
            "INSERT INTO client_requests (client_action_id, route, response_json, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(client_action_id) DO NOTHING",
            (client_action_id, route, json.dumps(response, separators=(",", ":")), now_ms),
        )
        return cur.rowcount == 1
