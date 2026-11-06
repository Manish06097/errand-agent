# ADR 0013: Persistence layout

## Context

All durable state is SQLite. LangGraph's checkpointer can share a file with our tables (verified in the spike), but SQLite allows one writer per file even in WAL mode, and `durability="sync"` writes a checkpoint at every superstep.

## Decision

Three files: `agent.sqlite` (our business record, `synchronous=FULL`, guard transactions with `BEGIN IMMEDIATE`), `checkpoints.sqlite` (LangGraph), `world.sqlite` (the mock world, ADR 0004). JSONL traces sit beside them. Migrations are numbered SQL files applied at startup.

## Alternatives considered

- **One file for agent tables and checkpoints**: one backup unit, but checkpoint writes contend with the approval-consume and intent-insert transaction on the booking path, and there is no atomicity to gain because the checkpointer uses its own transactions.
- **Postgres**: unnecessary for a local-only system; the repository layer keeps it a port, not a redesign.

## Consequences

- No cross-file transactions, which is fine because the agent tables are authoritative.
- Checkpoints can be pruned or wiped without touching approvals, intents or memory.
