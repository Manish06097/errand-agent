-- agent.sqlite, schema 4 (N2, gate 2 decisions 9, 11 and 13): what forget, the mask and
-- the open-errand rebuild need on top of the memory tables of schema 1.

-- HMAC-keyed hashes of a forgotten value's surface forms and of each redacted span's
-- normalised text. They drive the mask and the trace reader without plaintext. The key
-- is a per-install secret kept outside this file (data/memory_hmac.key).
CREATE TABLE memory_surface_hashes (
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  kind TEXT NOT NULL CHECK (kind IN ('surface_form','redacted_span')),
  hmac TEXT NOT NULL,
  PRIMARY KEY (memory_id, kind, hmac)
);

-- Moves on every memory mutation, so a run in flight can see its memory set changed.
ALTER TABLE users ADD COLUMN memory_epoch INTEGER NOT NULL DEFAULT 0;

-- The memory ids behind the brief fields at proposal time (JSON array), so a forget or
-- a correct can invalidate the pending cards that depend on the row.
ALTER TABLE approvals ADD COLUMN memory_ids_json TEXT;

-- 'forgotten' spans are also overwritten in messages.body_json; 'superseded' spans are
-- hidden from the model view only.
ALTER TABLE message_redactions ADD COLUMN kind TEXT NOT NULL DEFAULT 'forgotten'
  CHECK (kind IN ('forgotten','superseded'));
