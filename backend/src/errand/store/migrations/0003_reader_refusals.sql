-- agent.sqlite, schema 3 (M10 review): a refusal marker for the quarantined reader.
-- Keyed like reader_cache (provider, sha256 of the raw reviews, reader_model) but kept
-- apart from it: a marker never yields labels, it only stops the same refused prompt
-- from being sent again in a later run. A prompt or schema change moves reader_model,
-- so it is a new key and a new attempt.
CREATE TABLE reader_refusals (
  provider_id TEXT NOT NULL,
  reviews_sha256 TEXT NOT NULL,
  reader_model TEXT NOT NULL,
  PRIMARY KEY (provider_id, reviews_sha256, reader_model)
);
