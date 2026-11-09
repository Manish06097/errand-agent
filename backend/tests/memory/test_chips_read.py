"""The chips in the conversation read: references resolved with their status at read time,
and no value once the row is no longer eligible."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.policies import context as context_policies
from evals.runner import harness
from tests.api.support import world


async def _chips(h: Any, kind: str) -> list[dict[str, Any]]:
    read = (await h.client.get(f"/api/conversations/{h.conversation_id}")).json()
    return [m["body"] for m in read["messages"] if m["body"].get("type") == kind]


async def test_a_saved_chip_reads_as_forgotten_with_no_value_after_a_forget(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world(), context_policies.memory_policy) as h:
        await h.say("From now on, tell them to use gate code ZEBRA-4417.")
        (chip,) = await _chips(h, "memory_saved")
        assert chip["memory"]["status"] == "active"
        assert chip["memory"]["label"] is not None and "ZEBRA" in chip["memory"]["label"]

        done = await h.client.delete(
            f"/api/memory/{chip['memory_id']}", params={"client_action_id": "d1"}
        )
        assert done.status_code == 200
        read = (await h.client.get(f"/api/conversations/{h.conversation_id}")).json()
        (after,) = await _chips(h, "memory_saved")
        assert after["memory"]["status"] == "forgotten" and after["memory"]["label"] is None
        assert "ZEBRA" not in str(read)


async def test_the_list_card_reads_the_active_rows_and_the_pending_suggestions(
    tmp_path: Path,
) -> None:
    async with harness(
        tmp_path, world(), context_policies.memory_policy, memory_seed="memory_demo"
    ) as h:
        await h.say("What do you remember about me?")
        (card,) = await _chips(h, "memory_list")
        assert len(card["preferences"]) == len(card["memory_ids"]) == 8
        assert card["suggestions"] == []
        assert {p["status"] for p in card["preferences"]} == {"active"}
        gone = card["preferences"][0]["memory_id"]
        await h.client.delete(f"/api/memory/{gone}", params={"client_action_id": "d1"})
        (after,) = await _chips(h, "memory_list")
        assert gone not in [p["memory_id"] for p in after["preferences"]]
        assert len(after["preferences"]) == 7
