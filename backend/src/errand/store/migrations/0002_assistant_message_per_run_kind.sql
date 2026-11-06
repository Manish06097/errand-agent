-- agent.sqlite, schema 2 (M3): respond writes at most one assistant message per run and
-- kind, and upserts on this key, so respond re-running after a crash (a resumed node
-- starts again from its first line) can never post the same message twice (6.5).
-- The user's own message also carries the run_id it started, which is why the index
-- covers assistant rows only.
CREATE UNIQUE INDEX one_assistant_message_per_run_kind
  ON messages(run_id, kind) WHERE role = 'assistant' AND run_id IS NOT NULL;
