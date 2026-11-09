"""BUG-N2-1: a forgotten, memory-pre-filled area survived in a step-limit notice.

The LIMIT_REACHED notice restates the brief ("So far I have: plumber, Indiranagar, ...").
Round 2 took `notice` out of the redaction scope, so after DELETE /api/memory/{id} the
area stayed in messages.body_json and the builder replayed it into S6 on the next turn.

End to end: memory_demo style home_area, a turn that ends LIMIT_REACHED with the area
pre-filled, forget through DELETE, then read the database dump and the next request.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from evals.policies import reference
from evals.policies.common import read, tool_calls
from evals.runner import harness
from tests.api.support import OPENER, world


async def test_a_forgotten_area_is_gone_from_the_notice_and_from_the_next_request(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def record_then_loop(request: ScriptedRequest) -> AIMessage:
        seen.append("\n".join(str(m.content) for m in request.messages))
        view = read(request)
        if not view.loop:
            patch = reference.extract(view.current, view)
            return tool_calls(
                request, "Record it.", ("update_brief", reference.update_brief_args(patch))
            )
        return tool_calls(request, "Again.", ("list_bookings", {}))

    async with harness(tmp_path, world(), policy=record_then_loop) as h:
        saved = await h.client.post(
            "/api/memory",
            json={
                "key": "home_area", "scope": "global", "value": "indiranagar",
                "client_action_id": "a1",
            },
        )  # fmt: skip
        assert saved.status_code == 201
        first = await h.say(OPENER)
        notice = first.notice()
        assert first.outcome == "LIMIT_REACHED" and notice is not None
        assert "Indiranagar" in notice["text"]

        done = await h.client.delete("/api/memory/m-01", params={"client_action_id": "d1"})
        assert done.status_code == 200

        async with h.app.state.manager.db.session() as repos:
            dump = "\n".join([line async for line in repos.conn.iterdump()])
        assert "ndiranagar" not in dump

        seen.clear()
        await h.say("Please try again.")
        assert seen and not any("ndiranagar" in s for s in seen)
