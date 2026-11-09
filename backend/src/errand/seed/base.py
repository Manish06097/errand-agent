"""What both seeds share: the clock, the database, the message writer and the filler."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.clock import IST, to_ms
from errand.memory import hmac_key
from errand.memory.service import MemoryService
from errand.store.agent_db import AgentDB
from errand.store.repos.messages import Message
from errand.store.repos.users import User
from errand.world.rng import draw

USER_ID = "u_demo"
CONVERSATION_ID = "c_0001"
SEED_EPOCH = to_ms(datetime(2026, 8, 1, 9, 0, tzinfo=IST))
SESSION_LENGTH = 40  # messages per session of the rich seed
DAY_MS = 86_400_000
SEED_KEY_LABEL = "memory-seed-v1"
KEY_FILE = "memory_hmac.key"
CANARY = re.compile(r"CANARY-[A-Z]+-\d+")

FILLER_USER = (
    "Thanks, that works for me.",
    "Could you check the tap again?",
    "What did the last visit cost?",
    "Please keep it short.",
    "Is the technician coming with parts?",
    "Can you send me the details again?",
    "Okay, sounds good.",
    "I will be at home.",
    "One more question about the visit.",
    "Let me think about it.",
    "That was helpful.",
    "Do they carry an ID card?",
)
FILLER_ASSISTANT = (
    "Happy to help with that.",
    "Here is what I found.",
    "The visit usually takes about an hour.",
    "I will keep it brief.",
    "Let me know if you want to change anything.",
    "I can look at other options if you like.",
    "Noted. Anything else for this errand?",
    "They bring the usual tools and parts.",
    "I will not book anything until you approve.",
    "Understood.",
    "You are welcome.",
    "Sure, one moment.",
)


class SeedClock:
    """Simulated agent time for a seed: only the seeder moves it."""

    def __init__(self, start_ms: int = SEED_EPOCH) -> None:
        self.ms = start_ms

    def now_ms(self) -> int:
        return self.ms

    def step(self, ms: int = 60_000) -> int:
        self.ms += ms
        return self.ms


@dataclass
class Seeded:
    """A seed under construction: the database, the service and a message writer."""

    db: AgentDB
    service: MemoryService
    clock: SeedClock
    key: bytes
    messages: int = 0
    # canary -> where it was planted: {"kind": "message"|"errand"|"booking", "id": ...}
    planted: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    action_n: int = 0

    def action(self, label: str) -> str:
        self.action_n += 1
        return f"seed-{label}-{self.action_n:03d}"

    def plant(self, canary: str, kind: str, ident: str) -> None:
        self.planted.setdefault(canary, []).append({"kind": kind, "id": ident})

    async def say(
        self,
        role: str,
        body: dict[str, Any] | str,
        *,
        kind: str = "text",
        step_ms: int = 60_000,
    ) -> str:
        """Insert the next message (`msg_0001`, ...) and return its id."""
        self.messages += 1
        message_id = f"msg_{self.messages:04d}"
        if self.messages > 1 and (self.messages - 1) % SESSION_LENGTH == 0:
            self.clock.step(DAY_MS)  # a new session starts the next day
        payload = {"type": "text", "text": body} if isinstance(body, str) else body
        for canary in dict.fromkeys(CANARY.findall(json.dumps(payload))):
            self.plant(canary, "message", message_id)
        async with self.db.session() as repos:
            await repos.messages.insert(
                Message(
                    message_id,
                    CONVERSATION_ID,
                    f"seed-{message_id}" if role == "user" else None,
                    role,  # type: ignore[arg-type]
                    kind,  # type: ignore[arg-type]
                    payload,
                    None,
                    self.messages,
                    None,
                    None,
                    None,
                    self.clock.step(step_ms),
                )
            )
        return message_id

    async def filler(self, n: int) -> None:
        """`n` neutral messages: user and assistant alternating, from fixed phrase tables."""
        for i in range(n):
            is_user = i % 2 == 0
            table = FILLER_USER if is_user else FILLER_ASSISTANT
            pick = int(draw(11, "seed.filler", str(self.messages + 1), 0) * len(table))
            await self.say("user" if is_user else "assistant", table[pick])


async def open_seed(data_dir: Path) -> Seeded:
    """A fresh data directory: agent.sqlite with the user and the demo conversation."""
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / KEY_FILE
    if key_path.exists():
        # The seed hashes under its fixed (public) key. An install key left in place would
        # disagree with those hashes, and a public key must never become an install's.
        raise FileExistsError(f"{key_path} already exists; seed into a new directory")
    key = hmac_key.fixed_key(SEED_KEY_LABEL)
    key_path.write_text(key.hex(), encoding="ascii")
    key_path.chmod(0o600)
    db = await AgentDB.open(data_dir / "agent.sqlite")
    clock = SeedClock()
    async with db.session() as repos:
        await repos.users.upsert(User(USER_ID, "Asia/Kolkata", "Demo"))
        await repos.conversations.create(
            conversation_id=CONVERSATION_ID, user_id=USER_ID, now_ms=clock.now_ms()
        )
    service = MemoryService(db, clock.now_ms, key)
    return Seeded(db, service, clock, key)


async def dump_database(db: AgentDB) -> str:
    """The whole database as SQL text, in a stable order (for the determinism test)."""
    async with db.session() as repos:
        return "\n".join([line async for line in repos.conn.iterdump()])


def dumps_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
