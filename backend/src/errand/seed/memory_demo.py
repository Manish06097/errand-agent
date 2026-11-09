"""memory_demo: eight realistic rows, no canaries (gate 2 decision 22).

For `make dev`, the recorded demo and the memory variants of S01 and S08:
- `home_area` Indiranagar, so S01 asks zero questions;
- a global `earliest_time` and an electrician `earliest_time` of 10:00, so the electrician
  variant of S08 ("same electrician as last time, not before 10") finds it;
- an electrician `budget_ceiling`, so S08 does not ask the budget;
- a plumber `avoid_provider`, a `language`, an `access_note` and a `contact_window`.

Every row comes from a seeded user message carrying its cue, through the same evidence
check the `remember` tool uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from errand.seed.base import USER_ID, Seeded, open_seed

# (key, scope, value, what the user said, the quote inside it)
ROWS: tuple[tuple[str, str, Any, str, str], ...] = (
    (
        "home_area",
        "global",
        "indiranagar",
        "Hi! Just so you know, I live in Indiranagar, so please keep everything close to home.",
        "I live in Indiranagar",
    ),
    (
        "earliest_time",
        "global",
        "08:00",
        "I never want anyone at the door before 8 am.",
        "I never want anyone at the door before 8 am",
    ),
    (
        "earliest_time",
        "service:electrician",
        "10:00",
        "For electricians, I always want them to start after 10 am.",
        "I always want them to start after 10 am",
    ),
    (
        "budget_ceiling",
        "service:electrician",
        1500,
        "My usual budget for an electrician is 1500 rupees.",
        "My usual budget for an electrician is 1500 rupees",
    ),
    (
        "avoid_provider",
        "service:plumber",
        ["P7"],
        "I never want P7 as my plumber again.",
        "I never want P7 as my plumber",
    ),
    (
        "language",
        "global",
        "hinglish",
        "From now on, please talk to me in Hinglish.",
        "From now on, please talk to me in Hinglish",
    ),
    (
        "access_note",
        "global",
        "Ring the bell twice and wait at the gate.",
        "From now on, tell them: Ring the bell twice and wait at the gate.",
        "From now on, tell them: Ring the bell twice and wait at the gate.",
    ),
    (
        "contact_window",
        "global",
        {"from": "09:00", "to": "19:00"},
        "My usual contact window is 09:00-19:00, please do not call outside it.",
        "My usual contact window is 09:00-19:00",
    ),
)


async def build_demo(data_dir: Path) -> Seeded:
    seeded = await open_seed(data_dir)
    for key, scope, value, text, quote in ROWS:
        message_id = await seeded.say("user", text)
        await seeded.service.remember_explicit(
            user_id=USER_ID,
            key=key,
            scope=scope,
            value=value,
            message_id=message_id,
            quote=quote,
            actor="seed",
        )
        await seeded.say("assistant", "Noted. I will remember that.")
    return seeded
