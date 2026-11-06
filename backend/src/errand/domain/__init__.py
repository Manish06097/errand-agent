"""Shared Pydantic types with no I/O: the brief, slots and shortlists, errands, inbound
events, run counters, plan results, outcomes and message bodies.

Everything here may end up in a LangGraph checkpoint, so every type a checkpoint can
hold must be reachable from `ThreadState` (strict msgpack allowlists only those), and
nothing generic is parametrised: a `Fact[Service]` comes back from a strict checkpoint
as a plain dict, so each fact type is a concrete subclass.

This package imports nothing from errand except `errand.world.contracts` (the wire
types `Slot`, `Service`, `Area`) and `errand.clock`, so the model gateway can share
`RunCounters` without pulling in the agent.
"""
