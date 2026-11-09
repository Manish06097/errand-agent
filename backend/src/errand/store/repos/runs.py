from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

RunStatus = Literal["running", "ended", "recovered"]
_COLUMNS = (
    'run_id, conversation_id, "trigger", status, outcome, model_calls, tool_calls, tokens_in, '
    "tokens_out, cost_usd, trace_path, started_at, ended_at"
)


@dataclass(frozen=True)
class RunTotals:
    model_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


@dataclass(frozen=True)
class Run:
    run_id: str
    conversation_id: str
    trigger: str
    status: RunStatus
    outcome: str | None
    totals: RunTotals
    trace_path: str | None
    started_at: int
    ended_at: int | None


def _row(row: Any) -> Run:
    return Run(
        row["run_id"],
        row["conversation_id"],
        row["trigger"],
        row["status"],
        row["outcome"],
        RunTotals(
            row["model_calls"],
            row["tool_calls"],
            row["tokens_in"],
            row["tokens_out"],
            row["cost_usd"],
        ),
        row["trace_path"],
        row["started_at"],
        row["ended_at"],
    )


class RunsRepo:
    """The runs index. Everything except `status` can be rebuilt from the traces."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def start(
        self,
        *,
        run_id: str,
        conversation_id: str,
        trigger: str,
        now_ms: int,
        trace_path: str | None = None,
    ) -> Run:
        await self._conn.execute(
            'INSERT INTO runs (run_id, conversation_id, "trigger", status, trace_path, started_at) '
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, conversation_id, trigger, trace_path, now_ms),
        )
        run = await self.get(run_id)
        assert run is not None
        return run

    async def end(self, run_id: str, *, outcome: str, totals: RunTotals, now_ms: int) -> bool:
        """Mark a running run ended. False if it was not running (already ended, recovered)."""
        cur = await self._conn.execute(
            "UPDATE runs SET status = 'ended', outcome = ?, model_calls = ?, tool_calls = ?, "
            "tokens_in = ?, tokens_out = ?, cost_usd = ?, ended_at = ? "
            "WHERE run_id = ? AND status = 'running'",
            (
                outcome,
                totals.model_calls,
                totals.tool_calls,
                totals.tokens_in,
                totals.tokens_out,
                totals.cost_usd,
                now_ms,
                run_id,
            ),
        )
        return cur.rowcount == 1

    async def mark_recovered(
        self, run_id: str, now_ms: int, outcome: str = "RUN_INTERRUPTED"
    ) -> bool:
        """A run the process died in: ended by the reconciler, with the outcome its
        `run.end` carries."""
        cur = await self._conn.execute(
            "UPDATE runs SET status = 'recovered', outcome = ?, ended_at = ? "
            "WHERE run_id = ? AND status = 'running'",
            (outcome, now_ms, run_id),
        )
        return cur.rowcount == 1

    async def get(self, run_id: str) -> Run | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list_running(self) -> list[Run]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE status = 'running' ORDER BY started_at, run_id"
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def list_for_conversation(self, conversation_id: str) -> list[Run]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE conversation_id = ? ORDER BY started_at, run_id",
            (conversation_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]
