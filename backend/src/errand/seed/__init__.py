"""Seed data for the memory feature (architecture 13.5, gate 2 decisions 22 to 24).

Two seeds, both built through the real code paths (the memory service, the message
repo, the real forget), never raw SQL:

- `memory_demo`: eight realistic rows and no canaries, for `make dev`, the recorded demo
  and the memory variants of S01 and S08.
- `memory_rich_v1`: the 60-row canary seed for the evals, with 240 messages over 6
  sessions, 6 archived errands and 30 upcoming bookings.

`python -m errand.seed {memory_demo|memory_rich_v1} --data-dir PATH` writes agent.sqlite
and the HMAC key into PATH. Both are deterministic: fixed ids (`m-01`..., `msg_0001`...),
a simulated clock from a fixed epoch and a fixed HMAC key, so two builds are byte for
byte equal.
"""
