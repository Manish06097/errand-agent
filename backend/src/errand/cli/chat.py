"""`errand chat`: the terminal fallback over the same API as the web app (17.3).

    uv run --project backend errand chat --api http://127.0.0.1:8000

It needs only uv: httpx and the standard library. It covers what the web covers: create
or resume a conversation, send a message, stream the run's SSE events as activity lines,
number the quick replies and slot buttons, approve and decline through the decision
route with the card's fingerprint, post a render ack for every card it prints, and stamp
`client_composed_at` (and the ack) on the agent clock it syncs from `X-Agent-Clock`, the
same way the browser does (never the wall clock).

Input, one line at a time: a number picks the numbered action under the last card, `a`
and `d` approve and decline the card that is waiting, `m` shows more providers, anything
else is sent as a message. End of input or `/quit` leaves. The server is the guard; this
client shows what it is told and writes no card text of its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, TextIO

import httpx

AGENT_CLOCK_HEADER = "X-Agent-Clock"
TOP = 3
JITTER_MS = 1000


class AgentClock:
    """The agent clock: the last reading plus time elapsed on a monotonic clock."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._reading: tuple[str, int] | None = None
        self._at = 0.0
        self._last = 0

    def sync_header(self, value: str | None) -> None:
        if not value:
            return
        parts = [p.strip() for p in value.split(";")]
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        if parts[0] in ("sim", "system_offset") and "now_ms" in fields:
            self.sync(parts[0], int(fields["now_ms"]))

    def sync(self, mode: str, now_ms: int) -> None:
        # Jitter between two responses must not run the clock backwards, but a real jump
        # back (a scenario reload moves the world's clock) is the new truth, not a glitch.
        if now_ms < self._last - JITTER_MS:
            self._last = 0
        self._reading = (mode, now_ms)
        self._at = self._monotonic()

    @property
    def synced(self) -> bool:
        return self._reading is not None

    def now_ms(self) -> int:
        if self._reading is None:
            raise RuntimeError("the agent clock has not been synced yet")
        mode, base = self._reading
        elapsed = 0.0 if mode == "sim" else max(0.0, self._monotonic() - self._at) * 1000
        self._last = max(self._last, int(base + elapsed))
        return self._last

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now_ms() / 1000, UTC).isoformat(timespec="milliseconds")


def is_run_in_progress(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "run_in_progress"


@dataclass
class Choice:
    """One numbered action under the last card."""

    label: str
    run: Callable[[], None]


@dataclass
class Session:
    api: str
    out: TextIO
    http: httpx.Client
    clock: AgentClock = field(default_factory=AgentClock)
    conversation_id: str = ""
    shown: set[str] = field(default_factory=set)
    choices: list[Choice] = field(default_factory=list)
    more: Callable[[], None] | None = None
    visible_card: dict[str, str] | None = None
    waiting_card: dict[str, Any] | None = None
    last_user_text: str = ""

    # ------------------------------------------------------------------ transport

    def call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        response = self.http.request(method, path, json=body)
        self.clock.sync_header(response.headers.get(AGENT_CLOCK_HEADER))
        return response

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    # ------------------------------------------------------------------ conversation

    def start(self, conversation_id: str | None) -> None:
        if conversation_id is None:
            response = self.call("POST", "/api/conversations")
            response.raise_for_status()
            conversation_id = str(response.json()["conversation_id"])
        self.conversation_id = conversation_id
        convo = self.fetch()
        if not self.clock.synced:
            clock = convo["clock"]
            self.clock.sync(clock["mode"], int(clock["now_ms"]))
        self.say(f"errand {conversation_id}. Say what you need; a number picks an action.")
        self.show_new(convo)

    def fetch(self) -> dict[str, Any]:
        response = self.call("GET", f"/api/conversations/{self.conversation_id}")
        response.raise_for_status()
        convo: dict[str, Any] = response.json()
        clock = convo["clock"]
        self.clock.sync(clock["mode"], int(clock["now_ms"]))
        return convo

    def send(self, text: str) -> None:
        # Captured at Send: the agent clock and the card on screen (8.3).
        body: dict[str, Any] = {
            "client_msg_id": f"cm_{uuid.uuid4().hex[:16]}",
            "text": text,
            "client_composed_at": self.clock.iso(),
        }
        if self.visible_card is not None:
            body["visible_card"] = self.visible_card
        self.run_after(
            self.post_with_retry(
                "POST", f"/api/conversations/{self.conversation_id}/messages", body
            )
        )

    def post_with_retry(self, method: str, path: str, body: Any) -> httpx.Response:
        response = self.call(method, path, body)
        for _ in range(40):
            if response.status_code != 409 or not is_run_in_progress(response):
                break
            time.sleep(0.2)
            response = self.call(method, path, body)
        return response

    def run_after(self, response: httpx.Response) -> None:
        if response.status_code not in (200, 202):
            self.say(f"! not sent: {response.status_code} {response.text[:200]}")
            return
        run_id = response.json().get("run_id")
        if run_id:
            self.stream(str(run_id))
            self.show_new(self.fetch())

    # ------------------------------------------------------------------ SSE

    def events(self, run_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        with self.http.stream("GET", f"/api/runs/{run_id}/events", timeout=None) as response:
            self.clock.sync_header(response.headers.get(AGENT_CLOCK_HEADER))
            name, data = "message", []
            for line in response.iter_lines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    yield name, json.loads("\n".join(data))
                    name, data = "message", []

    def stream(self, run_id: str) -> None:
        for name, data in self.events(run_id):
            if name == "trace" and data.get("type") == "tool.call":
                outcome = f"error {data['error']}" if data.get("error") else "ok"
                latency = data.get("latency_ms")
                timing = f" {round(latency)} ms" if isinstance(latency, int | float) else ""
                self.say(f"  . {data.get('tool')} {outcome}{timing}")
            if name == "run.finished":
                return

    # ------------------------------------------------------------------ rendering

    def show_new(self, convo: dict[str, Any]) -> None:
        messages = convo["messages"]
        last_assistant = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        self.visible_card = None
        for m in reversed(messages):
            body = m["body"]
            if m["role"] == "assistant" and body["type"] in ("action", "reminder"):
                if (m.get("approval") or {}).get("status") == "pending":
                    self.visible_card = {
                        "approval_id": body["approval_id"],
                        "fingerprint": body["fingerprint"],
                        "message_id": m["message_id"],
                    }
                break
        for m in messages:
            if m["message_id"] in self.shown:
                continue
            self.shown.add(m["message_id"])
            if m["role"] == "user":
                if body_text := m["body"].get("text"):
                    self.last_user_text = str(body_text)
                continue
            live = m is last_assistant
            self.render(m, live=live)
            if m["body"]["type"] != "text":
                self.ack(m["message_id"])

    def ack(self, message_id: str) -> None:
        self.call("POST", f"/api/messages/{message_id}/rendered", {"client_ts": self.clock.iso()})

    def render(self, m: dict[str, Any], *, live: bool) -> None:
        body = m["body"]
        kind = body["type"]
        self.say()
        if live:
            self.choices, self.more, self.waiting_card = [], None, None
        if kind == "text":
            self.say(body["text"])
        elif kind == "clarify":
            self.say(body["question"])
            for option in body.get("options") or []:
                self.add_choice(live, option, partial(self.send, option))
        elif kind == "shortlist":
            self.render_shortlist(body, live)
        elif kind == "action":
            self.render_action(m, live)
        elif kind == "reminder":
            self.say(body["text"])
            if body.get("typed_grant_ok") is False:
                self.say(body["hint"])
            self.card_choices(m, body, live)
        elif kind == "outcome":
            mark = "  [verified]" if body["verified"] else ""
            self.say(f"{body['title']}{mark}")
            self.say(body["text"])
            for line in body.get("lines") or []:
                self.say(f"  {line['label']}: {line['value']}")
        elif kind == "notice":
            self.say(body["text"])
            for action in body.get("actions") or []:
                self.add_choice(live, action["label"], partial(self.notice_action, action))
        if body.get("status_line") and not (kind == "action" and not live):
            self.say(f"  ({body['status_line']})")

    def add_choice(self, live: bool, label: str, run: Callable[[], None]) -> None:
        if not live:
            return
        self.choices.append(Choice(label, run))
        self.say(f"  [{len(self.choices)}] {label}")

    def render_shortlist(self, body: dict[str, Any], live: bool, show_all: bool = False) -> None:
        if body.get("recommendation"):
            self.say(body["recommendation"])
        self.say(body["window_label"])
        candidates = body["candidates"]
        shown = candidates if show_all else candidates[:TOP]
        for c in shown:
            badge = " [verified]" if c["verified"] else ""
            self.say(f"- {c['name']}  {c['rating']:.1f}  Rs {c['price_inr']}{badge}")
            if c.get("notice"):
                self.say(f"    ! {c['notice']}")
            for slot in c["slots"]:
                self.add_choice(live, f"{slot['label']} ({c['name']})", partial(self.select, slot))
        if body.get("excluded_count"):
            self.say(f"  {body['excluded_count']} excluded")
        if live and len(candidates) > TOP and not show_all:
            self.say(f"  [m] show {len(candidates) - TOP} more")
            self.more = lambda: self.render_shortlist(body, live, show_all=True)

    def select(self, slot: dict[str, Any]) -> None:
        body = {
            "client_action_id": f"ca_{uuid.uuid4().hex[:16]}",
            "kind": "select_slot",
            "payload": {"slot_ref": slot["ref"]},
        }
        self.run_after(
            self.post_with_retry("POST", f"/api/conversations/{self.conversation_id}/actions", body)
        )

    def render_action(self, m: dict[str, Any], live: bool) -> None:
        body = m["body"]
        status = (m.get("approval") or {}).get("status", "pending")
        self.say(f"{body['title']}  [{status}]")
        for line in body["lines"]:
            self.say(f"  {line['label']}: {line['value']}")
        if status == "pending":
            self.card_choices(m, body, live)

    def card_choices(self, m: dict[str, Any], body: dict[str, Any], live: bool) -> None:
        if (m.get("approval") or {}).get("status", "pending") != "pending" or not live:
            return
        card = {"approval_id": body["approval_id"], "fingerprint": body["fingerprint"]}
        self.waiting_card = card
        self.add_choice(live, body["button_label"], lambda: self.decide("approve", card))
        self.add_choice(
            live, body.get("decline_label", "Decline"), lambda: self.decide("decline", card)
        )

    def decide(self, decision: str, card: dict[str, Any]) -> None:
        response = self.call(
            "POST",
            f"/api/approvals/{card['approval_id']}/decision",
            {
                "decision": decision,
                "fingerprint": card["fingerprint"],
                "client_action_id": f"ca_{uuid.uuid4().hex[:16]}",
            },
        )
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            self.say(f"! not decided: {response.status_code} {response.text[:200]}")
            self.show_new(self.fetch())
            return
        if response.status_code == 202 and answer.get("run_id"):
            self.stream(str(answer["run_id"]))
        elif response.status_code == 200:
            self.say(f"already {answer.get('status', 'approved')}")
        else:
            error = str(answer.get("error", response.status_code))
            known_stale = error == "details_changed" or (
                error == "no_longer_valid" and answer.get("status") in ("superseded", "invalidated")
            )
            self.say("! details changed" if known_stale else f"! {error.replace('_', ' ')}")
        self.show_new(self.fetch())

    def notice_action(self, action: dict[str, Any]) -> None:
        if action["kind"] == "refresh_hold" and action.get("approval_id"):
            body = {
                "client_action_id": f"ca_{uuid.uuid4().hex[:16]}",
                "kind": "refresh_hold",
                "payload": {"approval_id": action["approval_id"]},
            }
            self.run_after(
                self.post_with_retry(
                    "POST", f"/api/conversations/{self.conversation_id}/actions", body
                )
            )
        elif action["kind"] == "answer":
            # Nobody types "Answer": hand the prompt back to the user and send nothing.
            self.say("Type your answer below.")
        elif action["kind"] == "shorten":
            self.say("Type a shorter version of your message below. Your last message was:")
            self.say(f"  {self.last_user_text}")
        else:
            self.send(action["label"])

    # ------------------------------------------------------------------ input

    def handle(self, line: str) -> bool:
        """One line of input. False means leave."""
        text = line.strip()
        if not text:
            return True
        if text in ("/quit", "/q"):
            return False
        if text.isdigit() and 1 <= int(text) <= len(self.choices):
            self.choices[int(text) - 1].run()
        elif text == "a" and self.waiting_card is not None:
            self.decide("approve", self.waiting_card)
        elif text == "d" and self.waiting_card is not None:
            self.decide("decline", self.waiting_card)
        elif text == "m" and self.more is not None:
            self.more()
        else:
            self.send(text)
        return True


def chat(api: str, conversation: str | None, stdin: TextIO, stdout: TextIO) -> int:
    with httpx.Client(base_url=api.rstrip("/"), timeout=60) as http:
        session = Session(api=api, out=stdout, http=http)
        try:
            session.start(conversation)
        except httpx.HTTPError as exc:
            print(f"errand chat: cannot reach {api}: {exc}", file=sys.stderr)
            return 2
        while True:
            if stdin.isatty():
                print("> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line or not session.handle(line):
                break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="errand", description="Errand agent command line.")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_p = sub.add_parser("chat", help="chat with the agent in the terminal")
    chat_p.add_argument("--api", default="http://127.0.0.1:8000", help="the errand-api base URL")
    chat_p.add_argument("--conversation", default=None, help="resume this conversation id")
    args = parser.parse_args(argv)
    return chat(args.api, args.conversation, sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
