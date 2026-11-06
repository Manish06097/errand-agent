"""Repositories over agent.sqlite.

Only what M3 needs next: users, conversations, messages and runs. Approvals, intents,
memories and the rest have their schema already; their repositories arrive with the
milestone that owns their conditional UPDATEs, next to the tests for those rules.

Callers pass ids and `now_ms` in: the IdFactory and the Clock are M1's.
"""
