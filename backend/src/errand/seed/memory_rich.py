"""memory_rich_v1: the 60-row canary seed (architecture 13.5, gate 2 decisions 23 and 24).

20 active explicit, 4 active inferred and confirmed, 8 active but irrelevant (pest control
and painting scopes, plus `contact_window`, which feeds follow-ups only), 8 superseded
(one three-version `home_area` chain), 7 forgotten, 9 inferred pending, 4 rejected (2
dismissed, 1 undone, 1 `superseded_by_explicit`). Also 240 messages over 6 sessions in
the demo thread, 6 archived errands, 30 upcoming bookings and the E4 and A2 bookings.

Conflicts on purpose: the `home_area` chain; a global and an electrician `earliest_time`;
an explicit plumber `avoid_provider` P7 with two past P7 bookings, so the rule must not
suggest P7; a pending inference rejected by a later explicit write (A5).

Canaries go only where text exists. A forgotten typed row says its canary in the evidence
quote, and the same sentence is repeated in other user messages, assistant messages, a
card body, an archived brief and a booking's `origin_brief_json`; a forgotten
`access_note` is its own text. Some of those sit inside the last six turns of the
thread, so redaction and the mask do real work in S6. Superseded rows carry a canary in
their evidence span, also inside the last six turns. The undone row's canary is in the
user's own message, which the manifest marks `user_words_kept`.

Everything is written through `MemoryService`, the message repo, the archive repo and the
real `forget`; nothing is raw SQL. The forgets run last, so every echo already exists.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.clock import IST, to_ms
from errand.domain.brief import Brief, BudgetFact, Provenance, TextFact
from errand.memory import inference
from errand.seed.base import (
    CONVERSATION_ID,
    USER_ID,
    Seeded,
    dump_database,
    dumps_manifest,
    open_seed,
)
from errand.store.repos.errands import ArchivedErrand
from errand.store.repos.known_bookings import KnownBooking

TOTAL_MESSAGES = 240
SESSIONS = 6

NOTE_UNDONE = "leave it with CANARY-UNDONE-5521"
NOTE_F1 = "CANARY-FORGOTTEN-7731 is the gate code"
NOTE_F2 = "CANARY-FORGOTTEN-7732 leave the parcel with the guard"
NOTE_S1 = "CANARY-SUPERSEDED-1104 ring twice"
NOTE_HEAD = "Ring the bell twice and wait at the gate."

# (key, scope, value, quote) - the row is `explicit`, written from a user message that is
# exactly the quote prefixed by nothing: the quote is the whole message.
FORGOTTEN_TYPED: tuple[tuple[str, str, str, Any, str], ...] = (
    (
        "CANARY-FORGOTTEN-7733",
        "earliest_time",
        "service:cleaning",
        "11:00",
        "From now on no cleaning visits before 11 am CANARY-FORGOTTEN-7733",
    ),
    (
        "CANARY-FORGOTTEN-7734",
        "budget_ceiling",
        "service:carpentry",
        700,
        "I never pay more than ₹700 for carpentry CANARY-FORGOTTEN-7734",
    ),
    (
        "CANARY-FORGOTTEN-7735",
        "avoid_days",
        "service:gardening",
        ["thursday"],
        "I never want gardeners on Thursdays CANARY-FORGOTTEN-7735",
    ),
    (
        "CANARY-FORGOTTEN-7736",
        "latest_time",
        "service:laundry",
        "17:00",
        "I always want laundry pickups done by 5 pm CANARY-FORGOTTEN-7736",
    ),
    (
        "CANARY-FORGOTTEN-7737",
        "earliest_time",
        "service:tutoring",
        "16:00",
        "From now on no tutors before 4 pm CANARY-FORGOTTEN-7737",
    ),
)

# Written first and replaced by a correction: (key, scope, value, quote) then the new one.
CORRECTIONS: tuple[tuple[tuple[str, str, Any, str], tuple[str, str, Any, str]], ...] = (
    (
        (
            "earliest_time",
            "global",
            "09:00",
            "I always want visits to start after 9 am CANARY-SUPERSEDED-1103",
        ),
        ("earliest_time", "global", "08:00", "From now on, start after 8 am"),
    ),
    (
        ("budget_ceiling", "service:plumber", 1000, "I never spend more than ₹1,000 on a plumber"),
        ("budget_ceiling", "service:plumber", 1200, "I never spend more than ₹1,200 on a plumber"),
    ),
    (
        (
            "budget_ceiling",
            "service:electrician",
            1200,
            "I never spend more than ₹1,200 on an electrician",
        ),
        (
            "budget_ceiling",
            "service:electrician",
            1500,
            "I never spend more than ₹1,500 on an electrician",
        ),
    ),
    (
        (
            "avoid_days",
            "global",
            ["saturday"],
            "I never want visits on Saturdays CANARY-SUPERSEDED-1105",
        ),
        ("avoid_days", "global", ["sunday"], "I never want visits on Sundays"),
    ),
    (
        ("language", "global", "english", "I always want replies in English"),
        ("language", "global", "hinglish", "From now on, reply in Hinglish"),
    ),
)

# Active explicit rows with no history.
SINGLES: tuple[tuple[str, str, Any, str], ...] = (
    (
        "earliest_time",
        "service:electrician",
        "10:00",
        "For electricians, I always want them after 10 am",
    ),
    (
        "budget_ceiling",
        "service:ac_servicing",
        2500,
        "I never spend more than ₹2,500 on AC servicing",
    ),
    ("avoid_days", "service:plumber", ["saturday"], "I never want plumbers on Saturdays"),
    ("avoid_days", "service:electrician", ["friday"], "I never want electricians on Fridays"),
    ("avoid_days", "service:ac_servicing", ["monday"], "I never want AC servicing on Mondays"),
    ("avoid_provider", "service:plumber", ["P7"], "I never want P7 as my plumber"),
    ("avoid_provider", "service:electrician", ["E3"], "I never want E3 as my electrician"),
    ("earliest_time", "service:plumber", "07:30", "From now on plumbers may come from 07:30"),
    ("latest_time", "service:plumber", "20:00", "I always want plumbers done by 8 pm"),
    ("latest_time", "service:electrician", "19:00", "I always want electricians done by 7 pm"),
    ("earliest_time", "service:ac_servicing", "09:00", "I never want AC servicing before 9 am"),
    ("latest_time", "service:ac_servicing", "18:00", "I always want AC servicing done by 6 pm"),
)

# Active but irrelevant to a booking (other services, and a follow-up key).
IRRELEVANT: tuple[tuple[str, str, Any, str], ...] = (
    ("budget_ceiling", "service:pest_control", 900, "I never spend more than ₹900 on pest control"),
    ("avoid_days", "service:pest_control", ["tuesday"], "I never want pest control on Tuesdays"),
    ("earliest_time", "service:pest_control", "09:00", "I never want pest control before 9 am"),
    ("latest_time", "service:pest_control", "17:00", "I always want pest control done by 5 pm"),
    ("budget_ceiling", "service:painting", 5000, "I never spend more than ₹5,000 on painting"),
    ("avoid_days", "service:painting", ["wednesday"], "I never want painters on Wednesdays"),
    ("earliest_time", "service:painting", "10:00", "From now on painters may come after 10 am"),
    (
        "contact_window",
        "global",
        {"from": "09:00", "to": "19:00"},
        "My usual contact window is 09:00-19:00",
    ),
)

PLUMBERS_PENDING = ("P2", "P9", "P11")
ELECTRICIANS_PENDING = ("E2", "E5", "E10")
AC_PENDING = ("A2", "A3", "A4")


class Rich:
    """The build, step by step; `ids` maps a label to the memory id it got."""

    def __init__(self, seeded: Seeded) -> None:
        self.s = seeded
        self.ids: dict[str, str] = {}
        self.stmt: dict[str, str] = {}  # label -> id of the user's statement message
        self.forgotten_ids: list[str] = []
        self.declined: list[dict[str, Any]] = []
        self.user_words_kept: list[dict[str, Any]] = []
        self.bookings_n = 0

    # ---------------------------------------------------------------- statements

    async def state(
        self, label: str, key: str, scope: str, value: Any, quote: str, text: str | None = None
    ) -> str:
        """A user message (the quote, or `text` containing it) and the row it saves."""
        message_id = await self.s.say("user", text or quote)
        got = await self.s.service.remember_explicit(
            user_id=USER_ID, key=key, scope=scope, value=value,
            message_id=message_id, quote=quote, actor="seed",
        )  # fmt: skip
        memory_id = str(got["memory"]["memory_id"])
        self.ids[label] = memory_id
        self.stmt[label] = message_id
        await self.s.say("assistant", "Noted. I will remember that.")
        return memory_id

    async def rule_rows(
        self, service: str, providers: Sequence[str], bookings: list[KnownBooking]
    ) -> None:
        for provider in providers:
            subset = [b for b in bookings if b.provider_id == provider]
            (cand,) = [
                c for c in inference.same_provider_twice(subset) if c.scope == f"service:{service}"
            ]
            got = await self.s.service.suggest(
                user_id=USER_ID, key=cand.key, scope=cand.scope, value=cand.value,
                rule=cand.rule, booking_ids=cand.booking_ids,
            )  # fmt: skip
            assert got is not None
            if got["declined"] is not None:
                self.declined.append(
                    {"scope": cand.scope, "value": cand.value, "why": got["declined"]}
                )
            else:
                self.ids[f"{service}:{provider}"] = str(got["memory"]["memory_id"])

    async def book(
        self, provider: str, service: str, day: int, hour: int, status: str = "completed",
        brief_json: str | None = None,
    ) -> KnownBooking:  # fmt: skip
        self.bookings_n += 1
        start = to_ms(datetime(2026, 9, 1, hour, 0, tzinfo=IST)) + (day - 1) * 86_400_000
        booking = KnownBooking(
            f"BK-{self.bookings_n:03d}", USER_ID, provider, service, start, start + 3_600_000,
            status, start, CONVERSATION_ID, brief_json, None,
        )  # fmt: skip
        async with self.s.db.transaction() as repos:
            await repos.known_bookings.upsert(booking)
        return booking

    async def pair(self, provider: str, service: str, first_day: int) -> list[KnownBooking]:
        return [
            await self.book(provider, service, first_day, 14),
            await self.book(provider, service, first_day + 20, 15),
        ]

    # ------------------------------------------------------------------- the script

    async def run(self, fillers: int) -> None:
        s = self.s
        await s.filler(fillers)

        # the undone row: the user's words stay, the row is rejected (decision 5)
        quote_undone = f"From now on, tell them: {NOTE_UNDONE}"
        msg = await s.say("user", quote_undone)
        got = await s.service.remember_explicit(
            user_id=USER_ID, key="access_note", scope="global", value=NOTE_UNDONE,
            message_id=msg, quote=quote_undone, actor="seed",
        )  # fmt: skip
        undone_id = str(got["memory"]["memory_id"])
        await s.say("assistant", "Noted. I will remember that.")
        await s.service.undo(
            user_id=USER_ID, memory_id=undone_id, client_action_id=s.action("undo")
        )
        self.ids["undone"] = undone_id
        self.user_words_kept.append(
            {"message_id": msg, "start": 0, "end": len(quote_undone), "memory_id": undone_id}
        )

        # booking history for the rules (past bookings, two per provider)
        plumbers = [
            b
            for p in ("P1", *PLUMBERS_PENDING, "P10", "P7")
            for b in await self.pair(p, "plumber", 2 + len(self.ids))
        ]
        electricians = [
            b
            for p in ("E1", *ELECTRICIANS_PENDING, "E9")
            for b in await self.pair(p, "electrician", 3 + len(self.ids))
        ]
        acs = [
            b
            for p in ("A1", *AC_PENDING, "A5")
            for b in await self.pair(p, "ac_servicing", 4 + len(self.ids))
        ]
        e4 = await self.book("E4", "electrician", 40, 10)
        mornings = [await self.book("P1", "plumber", 50 + i, 9) for i in range(4)]

        # explicit rows: corrections first (the predecessor, then the new version)
        for index, (old, new) in enumerate(CORRECTIONS):
            await self.state(f"pre{index}", *old)
            await self.state(f"cor{index}", *new)

        # the P7 story: an explicit avoid, then the rule over two P7 bookings must decline
        await self.state(
            "avoid_p7", "avoid_provider", "service:plumber", ["P7"], "I never want P7 as my plumber"
        )
        await self.rule_rows("plumber", ["P7"], plumbers)

        # suggestions, in the order that leaves each slot's mix as the manifest says
        for service, providers, history in (
            ("plumber", ("P1", *PLUMBERS_PENDING, "P10"), plumbers),
            ("electrician", ("E1", *ELECTRICIANS_PENDING, "E9"), electricians),
            ("ac_servicing", ("A1", *AC_PENDING, "A5"), acs),
        ):
            await self.rule_rows(service, providers, history)
        latest = inference.latest_time_before_noon(mornings, IST)
        assert latest is not None
        suggested = await s.service.suggest(
            user_id=USER_ID, key=latest.key, scope=latest.scope, value=latest.value,
            rule=latest.rule, booking_ids=latest.booking_ids,
        )  # fmt: skip
        assert suggested is not None and suggested["memory"] is not None
        self.ids["latest:12"] = str(suggested["memory"]["memory_id"])

        # what the user does with them
        for label in ("plumber:P1", "electrician:E1", "ac_servicing:A1", "latest:12"):
            await s.service.keep(
                user_id=USER_ID, memory_id=self.ids[label], client_action_id=s.action("keep")
            )
        for label in ("plumber:P10", "electrician:E9"):
            await s.service.dismiss(
                user_id=USER_ID, memory_id=self.ids[label], client_action_id=s.action("dismiss")
            )

        # the explicit avoid on A5 rejects the pending A5 suggestion (superseded_by_explicit)
        await self.state(
            "avoid_a5",
            "avoid_provider",
            "service:ac_servicing",
            ["A5"],
            "I never want A5 for AC servicing",
        )

        for index, (key, scope, value, quote) in enumerate(SINGLES):
            if (key, scope) == ("avoid_provider", "service:plumber"):
                continue  # already written above
            await self.state(f"single{index}", key, scope, value, quote)
        for index, (key, scope, value, quote) in enumerate(IRRELEVANT):
            await self.state(f"irrelevant{index}", key, scope, value, quote)

        # home_area chain, notes chain and the forgotten typed rows, with their echoes
        await self.state(
            "home1",
            "home_area",
            "global",
            "whitefield",
            "I live in Whitefield CANARY-SUPERSEDED-1101",
        )
        await self.state(
            "note1", "access_note", "global", NOTE_F1, f"From now on, tell them: {NOTE_F1}"
        )
        await self.echo(NOTE_F1, "CANARY-FORGOTTEN-7731")
        await self.state(
            "note2", "access_note", "global", NOTE_F2, f"From now on, tell them: {NOTE_F2}"
        )
        await self.echo(NOTE_F2, "CANARY-FORGOTTEN-7732")
        for index, (canary, key, scope, value, quote) in enumerate(FORGOTTEN_TYPED):
            await self.state(f"forgotten{index}", key, scope, value, quote)
            await self.echo(quote, canary)
        await self.archive(bookings=[e4])

        await self.tail()
        await self.forget_all()

    async def echo(self, text: str, canary: str) -> None:
        """The same sentence in another user message, an assistant message and a card."""
        s = self.s
        await s.say("user", f"Earlier I said: {text}")
        await s.say("assistant", f"You told me: {text}")
        await s.say(
            "assistant",
            {
                "type": "clarify",
                "errand_id": "e_seed_0",
                "fields": ["area"],
                "question": f"Should I still apply this? {text}",
                "options": [],
            },
            kind="card",
        )

    async def archive(self, bookings: list[KnownBooking]) -> None:
        """Six archived errands; those that carry a forgotten row's sentence as evidence."""
        s = self.s
        sentences = [
            (f"forgotten{i}", FORGOTTEN_TYPED[i][0], FORGOTTEN_TYPED[i][4]) for i in range(5)
        ]
        sentences.append(("note1", "CANARY-FORGOTTEN-7731", NOTE_F1))
        for n, (label, canary, text) in enumerate(sentences, start=1):
            brief = Brief(
                issue=TextFact(
                    value="kitchen tap",
                    prov=Provenance(source="memory", memory_id=self.ids[label], evidence=text),
                    set_at_turn=1,
                ),
                budget_max_inr=BudgetFact(
                    value=1200,
                    prov=Provenance(source="user_msg", evidence="under 1200"),
                    set_at_turn=1,
                ),
            )
            async with s.db.transaction() as repos:
                await repos.errands.archive(
                    ArchivedErrand(
                        f"e_seed_{n}", CONVERSATION_ID, "book", "completed",
                        brief.model_dump_json(), None, "BOOKED_VERIFIED",
                        s.clock.now_ms(), s.clock.now_ms(),
                    ),
                    USER_ID,
                )  # fmt: skip
            s.plant(canary, "errand", f"e_seed_{n}")
            origin = await self.book(
                "P1", "plumber", 70 + n, 10, "completed", brief.model_dump_json()
            )
            s.plant(canary, "booking", origin.booking_id)
        # 30 upcoming bookings (S3's sub-caps) and the A2 AC servicing for "move it to Saturday"
        for i in range(29):
            provider = ("P2", "E2", "A3")[i % 3]
            service = ("plumber", "electrician", "ac_servicing")[i % 3]
            await self.book(provider, service, 70 + i, 10 + i % 6, "confirmed")
        await self.book("A2", "ac_servicing", 71, 11, "confirmed")

    async def tail(self) -> None:
        """The last six turns of the thread: canaries inside the window S6 renders."""
        s = self.s
        await s.say("user", f"Please remember this for later: {FORGOTTEN_TYPED[0][4]}")
        await s.say("assistant", "Noted.")
        await self.state(
            "home2",
            "home_area",
            "global",
            "jayanagar",
            "I moved to Jayanagar CANARY-SUPERSEDED-1102",
        )
        await self.state(
            "note_s1", "access_note", "global", NOTE_S1, f"From now on, tell them: {NOTE_S1}"
        )
        await self.state("home3", "home_area", "global", "indiranagar", "I've moved to Indiranagar")
        await s.say(
            "assistant",
            {
                "type": "clarify",
                "errand_id": "e_seed_0",
                "fields": ["area"],
                "question": f"Is this still right? {FORGOTTEN_TYPED[1][4]}",
                "options": [],
            },
            kind="card",
        )
        await self.state(
            "note_head", "access_note", "global", NOTE_HEAD, f"From now on, tell them: {NOTE_HEAD}"
        )
        await s.say("assistant", f"You also asked me to pass on: {NOTE_F2}")
        await s.say("user", "Okay, thanks.")
        await s.say("assistant", "You are welcome.")

    async def forget_all(self) -> None:
        s = self.s
        for label in ("note2", *[f"forgotten{i}" for i in range(5)]):
            out = await s.service.forget(
                user_id=USER_ID, memory_id=self.ids[label], client_action_id=s.action("forget")
            )
            self.forgotten_ids += out["forgotten"]


async def _build_into(data_dir: Path, fillers: int) -> tuple[Rich, Seeded]:
    seeded = await open_seed(data_dir)
    rich = Rich(seeded)
    await rich.run(fillers)
    return rich, seeded


async def build_rich(data_dir: Path) -> tuple[Seeded, dict[str, Any]]:
    """Build memory_rich_v1 into `data_dir`; returns the seed and its manifest.

    Two passes: the first, in a scratch directory, counts the scripted messages, so the
    real build can pad the thread with exactly enough neutral filler to reach 240.
    """
    with tempfile.TemporaryDirectory() as scratch:
        rich, seeded = await _build_into(Path(scratch), 0)
        scripted = seeded.messages
        await seeded.db.close()
    assert scripted <= TOTAL_MESSAGES, f"the script alone is {scripted} messages"
    rich, seeded = await _build_into(data_dir, TOTAL_MESSAGES - scripted)
    assert seeded.messages == TOTAL_MESSAGES, seeded.messages
    return seeded, await manifest_of(rich, seeded)


async def manifest_of(rich: Rich, seeded: Seeded) -> dict[str, Any]:
    async with seeded.db.session() as repos:
        rows = await repos.memories.for_user(USER_ID)
        blocklist = await repos.memories.blocklist(USER_ID)
        events = {
            r.memory_id: [e.type for e in await repos.memories.events(r.memory_id)] for r in rows
        }
    reasons: dict[str, str] = {}
    async with seeded.db.session() as repos:
        for r in rows:
            if r.status == "rejected":
                for e in await repos.memories.events(r.memory_id):
                    if e.type == "rejected":
                        reasons[r.memory_id] = str((e.details or {}).get("reason"))
    by_label = {v: k for k, v in rich.ids.items()}
    return {
        "seed": "memory_rich_v1",
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "messages": seeded.messages,
        "counts": {
            status: sum(1 for r in rows if r.status == status)
            for status in ("active", "pending_confirmation", "superseded", "forgotten", "rejected")
        },
        "rows": [
            {
                "id": r.memory_id,
                "label": by_label.get(r.memory_id),
                "key": r.key,
                "scope": r.scope,
                "kind": r.kind,
                "status": r.status,
                "rejected_because": reasons.get(r.memory_id),
                "events": events[r.memory_id],
                "value_hash": r.value_hash,
            }
            for r in rows
        ],
        "canaries": {
            canary: {"kind": canary.split("-")[1].lower(), "places": places}
            for canary, places in sorted(seeded.planted.items())
        },
        "user_words_kept": rich.user_words_kept,
        "blocklist": [{"key": k, "scope": s, "reason": why} for k, s, _, why in blocklist],
        "inference_declined": rich.declined,
    }


__all__ = ["build_rich", "dump_database", "dumps_manifest"]
