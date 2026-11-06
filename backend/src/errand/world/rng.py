"""Keyed randomness (architecture 5.4, ADR 0010).

Every draw is a pure function of what it is about. Adding an unrelated call never
changes which booking times out, and there is deliberately no RNG object to share.
"""

from __future__ import annotations

import hashlib
from typing import Literal

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"]
BOOK_OUTCOMES: tuple[BookOutcome, ...] = (
    "success",
    "slot_taken",
    "timeout_booked",
    "timeout_not_booked",
)


def draw(seed: int, op: str, subject: str, attempt: int) -> float:
    """A uniform float in [0, 1) keyed by (seed, op, subject, attempt)."""
    h = hashlib.sha256(f"{seed}|{op}|{subject}|{attempt}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def book_outcome(u: float, probs: dict[BookOutcome, float]) -> BookOutcome:
    """Map a draw onto the cumulative buckets, in the brief's order."""
    if not 0.0 <= u < 1.0:
        raise ValueError(f"draw {u} is outside [0, 1)")
    edge = 0.0
    for outcome in BOOK_OUTCOMES:
        edge += probs[outcome]
        if u < edge:
            return outcome
    # Float rounding can leave the last edge a hair under 1.0; never return a bucket
    # whose probability is zero.
    return next(o for o in reversed(BOOK_OUTCOMES) if probs[o] > 0)
