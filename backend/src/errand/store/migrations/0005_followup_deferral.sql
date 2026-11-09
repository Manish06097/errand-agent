-- agent.sqlite, schema 5 (FIN, gate 3 decisions 7 and 15 to 16): `followup_jobs` gains the
-- state 'deferred' with the moment it is delivered, and the reason a job ended.
--
-- SQLite cannot change a CHECK in place, so the table is rebuilt. Nothing references
-- followup_jobs by foreign key. Jobs that ended before this migration keep a NULL reason:
-- the reason lived only in the `followup.done` trace record until now.
CREATE TABLE followup_jobs_v5 (
  job_id TEXT PRIMARY KEY,
  booking_id TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('detected','deferred','notified','ignored','done')),
  conversation_id TEXT,
  updated_at INTEGER NOT NULL,
  -- epoch ms of the contact window's next opening; set exactly while the job is deferred
  deliver_at INTEGER,
  done_reason TEXT CHECK (done_reason IN ('replaced','dismissed','lapsed')),
  UNIQUE (booking_id, event_kind),
  CHECK ((state = 'deferred') = (deliver_at IS NOT NULL)),
  CHECK (done_reason IS NULL OR state = 'done')
);

INSERT INTO followup_jobs_v5 (job_id, booking_id, event_kind, state, conversation_id, updated_at)
  SELECT job_id, booking_id, event_kind, state, conversation_id, updated_at FROM followup_jobs;

DROP TABLE followup_jobs;

ALTER TABLE followup_jobs_v5 RENAME TO followup_jobs;
