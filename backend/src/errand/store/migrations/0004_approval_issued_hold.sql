-- agent.sqlite, schema 4 (M5 review): the hold a card was issued on.
-- `bound_hold_id` moves when the one automatic re-hold per approval happens; this column
-- never does, so "was this approval already moved" is an identity comparison of the two
-- and no longer an inference from expiry times. Existing rows start with the hold they
-- are bound to now (a card that was already re-held is not recoverable, and none exists
-- outside a dev database).
ALTER TABLE approvals ADD COLUMN issued_hold_id TEXT;
UPDATE approvals SET issued_hold_id = bound_hold_id;
