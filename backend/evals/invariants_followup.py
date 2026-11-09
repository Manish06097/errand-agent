"""INV-17, as amended by Gate 3 decision 17: the follow-up never acts without the user.

It reads the records and nothing else (no product code), and each clause has a negative
test on doctored records (tests/evals/test_invariants_followup.py):

1. every follow-up job ends `notified`, `ignored` or `done`; a job still `detected` at the end
   of a scenario was never delivered (a `deferred` job, once N2's contact_window lands, is
   fine only with `deliver_at` after the scenario clock: TODO N2);
2. no `model.call` and no `reader.call` record exists in a follow-up conversation before its
   first user message: nothing is spent on the user's behalf until they reply;
3. no `book` in a follow-up conversation is preceded by anything but a grant made after the
   job was detected: its intent's approval is a consumed card of that conversation, granted by
   the user (button or typed) after the conversation (which delivery creates after detection)
   existed;
4. one notification per job: its `event_id` is the job id, once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evals.invariants import Records, Violations

ENDED = frozenset({"notified", "ignored", "done"})


def inv17_follow_up_acts_only_on_a_grant(rec: Records) -> Violations:
    out: Violations = []
    for job in rec.followup_jobs:
        if job["state"] not in ENDED:
            out.append(f"job {job['job_id']} ended {job['state']}, not notified, ignored or done")
    ids = [n["event_id"] for n in rec.notifications]
    for event_id in sorted({i for i in ids if ids.count(i) > 1}):
        out.append(f"{ids.count(event_id)} notifications for job {event_id}")
    for n in rec.notifications:
        if n["event_id"] not in {j["job_id"] for j in rec.followup_jobs}:
            out.append(f"notification {n['notification_id']} names no job")
    followups = {c["conversation_id"]: c for c in rec.conversations if c["kind"] == "followup"}
    user_run_index: dict[str, int] = {}
    for i, run in enumerate(rec.runs):
        conversation = run["conversation_id"]
        if conversation not in followups:
            continue
        start = next(
            (r for r in rec.traces.get(run["run_id"], []) if r["type"] == "run.start"), None
        )
        if (
            start is not None
            and start.get("user_message_id")
            and conversation not in user_run_index
        ):
            user_run_index[conversation] = i
    for i, run in enumerate(rec.runs):
        conversation = run["conversation_id"]
        if conversation not in followups:
            continue
        first_user = user_run_index.get(conversation)
        if first_user is not None and i >= first_user:
            continue
        for record in rec.traces.get(run["run_id"], []):
            if record["type"] in ("model.call", "reader.call"):
                out.append(
                    f"{run['run_id']}: a {record['type']} in follow-up conversation "
                    f"{conversation} before the user's first message"
                )
    approvals = {a["approval_id"]: a for a in rec.approvals}
    for intent in rec.intents:
        conversation = intent["conversation_id"]
        if conversation not in followups or intent["action"] != "book":
            continue
        approval = approvals.get(intent["approval_id"])
        if approval is None or approval["conversation_id"] != conversation:
            out.append(f"intent {intent['intent_id']}: no approval card in {conversation}")
            continue
        if approval["granted_at"] is None or approval["granted_via"] not in ("button", "typed"):
            out.append(
                f"intent {intent['intent_id']}: approval {approval['approval_id']} "
                "not granted by the user"
            )
        elif approval["granted_at"] < followups[conversation]["created_at"]:
            out.append(
                f"intent {intent['intent_id']}: granted before the follow-up conversation existed"
            )
    return out
