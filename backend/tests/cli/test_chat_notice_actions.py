"""`errand chat` notice actions and error handling (M7 review round 2).

"Answer" and "Shorten it" are asks for the user to type; they must never be posted as
the user's own words. A non-JSON error body must print, not crash.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import httpx

from errand.cli.chat import Session


def session(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[Session, io.StringIO, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    out = io.StringIO()
    http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(record))
    return Session(api="http://test", out=out, http=http, conversation_id="c_1"), out, seen


def nothing(_: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def test_answer_and_shorten_send_nothing() -> None:
    chat, out, seen = session(nothing)
    chat.last_user_text = "a very long message"
    chat.notice_action({"kind": "answer", "label": "Answer"})
    chat.notice_action({"kind": "shorten", "label": "Shorten it"})
    assert seen == []
    printed = out.getvalue()
    assert "Type your answer" in printed
    assert "a very long message" in printed


def test_a_non_json_error_on_decide_prints_the_status_instead_of_crashing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, text="<html>boom</html>")
        return httpx.Response(
            200,
            json={"messages": [], "clock": {"mode": "sim", "now_ms": 1}},
        )

    chat, out, _ = session(handler)
    chat.decide("approve", {"approval_id": "a_1", "fingerprint": "fp"})
    assert "500" in out.getvalue()
    assert "boom" in out.getvalue()
