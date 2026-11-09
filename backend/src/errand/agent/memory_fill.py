"""Memory in the brief at the start of a run (gate 2 decisions 10, 11 and 17).

`apply_memory` runs in `ingress`, before anything reads the brief. The rules are one pure
function, `errand.memory.apply.apply_memory`, which the follow-up's replacement brief calls
too; this module reads the rows and writes the `invalidate` records it reports:

1. **Clear.** A brief field whose `prov.memory_id` is no longer eligible is cleared, an
   `invalidate` record is written for it, and if the row was corrected, the field is
   re-filled from its active successor. This is how a forget or a correct that landed
   after the checkpoint was written reaches the open errand (the checkpoint is a
   different file from the forget's transaction). On that turn `missing()` contains the
   field and the agent asks for it (S49).
2. **Pre-fill.** At the start of an errand, a required field with a matching global row
   is filled by code with `prov.source = "memory"` and `prov.memory_id`, shown as
   "(from memory)". A field the user later clears is not refilled in that errand, and
   the current message beats memory because `update_brief` merges over it.

Service-scoped keys (a plumbing budget ceiling) are filled inside `update_brief`'s merge
when the service is set, because the service is not known yet at ingress (phase B).
"""

from __future__ import annotations

from errand.agent.deps import RunDeps
from errand.domain.errand import Errand
from errand.memory import apply


async def apply_memory(deps: RunDeps, errand: Errand, *, fresh: bool, turn: int) -> Errand:
    """The errand with stale memory fields cleared and global pre-fill applied."""
    async with deps.db.session() as repos:
        rows = await repos.memories.for_user(deps.user_id)
    applied = apply.apply_memory(
        errand.brief,
        rows,
        kind=errand.kind,
        fresh=fresh,
        now=deps.clock.now(),
        tz=deps.tz,
        turn=turn,
    )
    for inv in applied.invalidations:
        deps.tracer.emit("invalidate", what=inv.what, reason=inv.reason)
    if applied.brief is errand.brief:
        return errand
    return errand.model_copy(
        update={"brief": applied.brief.model_copy(update={"version": applied.brief.version + 1})}
    )
