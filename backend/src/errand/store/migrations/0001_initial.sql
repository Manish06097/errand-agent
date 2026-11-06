-- agent.sqlite, schema 1: every table in architecture section 4.1.
-- All timestamps are INTEGER epoch milliseconds (section 4). Constraints copied from
-- sections 8.2 (approvals), 9.2 (intents and attempts) and 12.1 (memory).
-- messages gets UNIQUE(run_id, kind) in M3, once respond defines the key (6.5).

CREATE TABLE users (
  user_id TEXT PRIMARY KEY,
  tz TEXT NOT NULL,
  display_name TEXT NOT NULL
);

CREATE TABLE conversations (
  conversation_id TEXT PRIMARY KEY,               -- = LangGraph thread_id
  user_id TEXT NOT NULL REFERENCES users(user_id),
  kind TEXT NOT NULL CHECK (kind IN ('chat','followup')),
  title TEXT,
  origin_booking_id TEXT,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL
);
CREATE INDEX conversations_by_user ON conversations(user_id, last_activity_at);

CREATE TABLE messages (
  message_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  client_msg_id TEXT UNIQUE,                      -- dedups retried POSTs
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  kind TEXT NOT NULL CHECK (kind IN ('text','card','notice','proactive')),
  body_json TEXT NOT NULL,                        -- memory values only as memory_id references
  run_id TEXT,
  turn_no INTEGER,
  approval_id TEXT,
  typed_grant_ok INTEGER CHECK (typed_grant_ok IN (0,1)),
  rendered_at INTEGER,                            -- client render ack for a card
  created_at INTEGER NOT NULL
);
CREATE INDEX messages_by_conversation ON messages(conversation_id, created_at);
CREATE INDEX messages_by_run ON messages(run_id);

CREATE TABLE message_redactions (
  message_id TEXT NOT NULL REFERENCES messages(message_id),
  "start" INTEGER NOT NULL,
  "end" INTEGER NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  PRIMARY KEY (message_id, "start", "end", memory_id),
  CHECK ("start" >= 0 AND "end" > "start")
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  "trigger" TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','ended','recovered')),
  outcome TEXT,
  model_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  trace_path TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE INDEX runs_by_conversation ON runs(conversation_id, started_at);
CREATE INDEX runs_by_status ON runs(status);

CREATE TABLE errands (
  errand_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  kind TEXT NOT NULL,
  phase TEXT NOT NULL,
  brief_json TEXT NOT NULL,
  replaces_booking_id TEXT,
  outcome TEXT,
  created_at INTEGER NOT NULL,
  closed_at INTEGER
);

CREATE TABLE holds (
  hold_record_id TEXT PRIMARY KEY,
  errand_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  slot_start INTEGER NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  hold_id TEXT NOT NULL,
  quoted_price_inr INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  UNIQUE (errand_id, provider_id, slot_start, generation)
);

-- section 8.2
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, errand_id TEXT NOT NULL,
  proposal_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL,
  fingerprint TEXT NOT NULL, material_json TEXT NOT NULL, card_json TEXT NOT NULL, bound_hold_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','granted','consumed','declined','invalidated','expired')),
  granted_via TEXT CHECK (granted_via IN ('button','typed')), granted_message_id TEXT,
  invalidated_reason TEXT, consumed_by_intent TEXT,
  requested_at INTEGER, granted_at INTEGER, consumed_at INTEGER, expires_at INTEGER NOT NULL);
CREATE UNIQUE INDEX one_live_approval ON approvals(conversation_id) WHERE status IN ('pending','granted');

-- section 9.2
CREATE TABLE action_intents (
  intent_id TEXT PRIMARY KEY, conversation_id TEXT, errand_id TEXT, user_id TEXT,
  action TEXT, approval_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, material_json TEXT,
  target_booking_id TEXT, replaces_booking_id TEXT,
  same_key_retries INTEGER NOT NULL DEFAULT 0,
  state TEXT CHECK (state IN ('open','succeeded','failed','needs_attention')),
  result_json TEXT, created_at INTEGER, updated_at INTEGER);
-- one live cancel or reschedule per booking, across all conversations
CREATE UNIQUE INDEX one_live_action_per_target ON action_intents(target_booking_id)
  WHERE action IN ('cancel','reschedule') AND state IN ('open','needs_attention');
-- one replacement per provider-cancelled booking, ever
CREATE UNIQUE INDEX one_replacement ON action_intents(replaces_booking_id)
  WHERE replaces_booking_id IS NOT NULL AND state IN ('open','needs_attention','succeeded');

CREATE TABLE intent_attempts (
  attempt_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES action_intents,
  n INTEGER NOT NULL, hold_id TEXT, hold_expires_at INTEGER, idempotency_key TEXT UNIQUE NOT NULL,
  state TEXT CHECK (state IN ('prepared','in_flight','unknown','applied','not_applied')),
  calls INTEGER DEFAULT 0, status_checks INTEGER DEFAULT 0, last_error TEXT, fence_epoch INTEGER NOT NULL,
  created_at INTEGER, updated_at INTEGER, UNIQUE(intent_id, n));
CREATE INDEX intent_attempts_by_state ON intent_attempts(state);

CREATE TABLE known_bookings (
  booking_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  service TEXT NOT NULL,
  slot_start INTEGER NOT NULL,
  slot_end INTEGER NOT NULL,
  status TEXT NOT NULL,
  last_seen_at INTEGER NOT NULL,
  origin_conversation_id TEXT,
  origin_brief_json TEXT,
  source_intent_id TEXT
);
CREATE INDEX known_bookings_by_user ON known_bookings(user_id, service, slot_start);

-- section 12.1, with every timestamp an INTEGER epoch ms (section 4)
CREATE TABLE memories (
  memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  scope TEXT NOT NULL,            -- 'global' | 'service:<name>'
  value_json TEXT,                -- NULL once forgotten
  value_hash TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('explicit','inferred')),
  status TEXT NOT NULL CHECK (status IN ('active','pending_confirmation','superseded','forgotten','rejected')),
  evidence_json TEXT,             -- {message_id, quote} | {rule, booking_ids}
  supersedes TEXT, superseded_by TEXT,
  created_at INTEGER NOT NULL, confirmed_at INTEGER, forgotten_at INTEGER, last_used_at INTEGER);
CREATE UNIQUE INDEX one_active ON memories(user_id, key, scope) WHERE status = 'active';

CREATE TABLE memory_events (      -- append-only
  event_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT,
  at INTEGER NOT NULL,
  details_json TEXT
);
CREATE INDEX memory_events_by_memory ON memory_events(memory_id, at);
CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events
BEGIN
  SELECT RAISE(ABORT, 'memory_events is append-only');
END;
CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events
BEGIN
  SELECT RAISE(ABORT, 'memory_events is append-only');
END;

CREATE TABLE memory_blocklist (   -- stops re-inference
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  scope TEXT NOT NULL,
  value_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (user_id, key, scope, value_hash)
);

CREATE VIEW memories_eligible AS SELECT * FROM memories WHERE status = 'active';

CREATE TABLE notifications (
  notification_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  conversation_id TEXT,
  kind TEXT NOT NULL,
  urgency TEXT NOT NULL,
  body_json TEXT NOT NULL,
  event_id TEXT UNIQUE,
  created_at INTEGER NOT NULL,
  read_at INTEGER
);

CREATE TABLE followup_jobs (
  job_id TEXT PRIMARY KEY,
  booking_id TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('detected','notified','ignored','done')),
  conversation_id TEXT,
  updated_at INTEGER NOT NULL,
  UNIQUE (booking_id, event_kind)
);

CREATE TABLE pending_resumes (
  approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id),
  conversation_id TEXT NOT NULL,
  inbound_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  started_run_id TEXT
);

CREATE TABLE leases (
  key TEXT PRIMARY KEY,           -- 'thread:<id>' or 'user:<id>'
  holder TEXT NOT NULL,
  epoch INTEGER NOT NULL,         -- +1 on every takeover; fences attempt-state writes
  expires_at INTEGER NOT NULL
);

CREATE TABLE reader_cache (
  provider_id TEXT NOT NULL,
  reviews_sha256 TEXT NOT NULL,
  output_json TEXT NOT NULL,
  reader_model TEXT NOT NULL,
  PRIMARY KEY (provider_id, reviews_sha256, reader_model)
);

CREATE TABLE client_requests (
  client_action_id TEXT PRIMARY KEY,
  route TEXT NOT NULL,
  response_json TEXT,
  created_at INTEGER NOT NULL
);
