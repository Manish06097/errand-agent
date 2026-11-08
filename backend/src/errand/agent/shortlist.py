"""From a search answer to the ranked, review-enriched shortlist (architecture 10.6, 11).

The order matters, because the loop is append-only and nothing is re-rendered later:
1. hard filters (budget, window), in search order;
2. a provisional score without `review_signal`;
3. code opens the top K candidates with `get_provider` as the RANKER caller, where K is
   what is left of this run's reader calls (ties by provider_id, through `rank`);
4. the quarantined reader labels their reviews (one concurrent batch);
5. the final score, with `review_signal` from the reader's labels;
6. the final order, then slot handles, then the search view.

A provider the reader could not read (no slot left in the cap, a timeout, a refusal,
a get_provider failure) gets no digest and no review bonus. Nothing in this path can
end the run: a reader or provider-detail outage costs the review signal, never the
shortlist. Only the search itself can fail the turn.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from errand.agent import ranking
from errand.agent.deps import RunDeps
from errand.domain.brief import TimeWindow
from errand.domain.slots import Shortlist
from errand.quarantine.reader import ReadResult
from errand.tools import views
from errand.tools.errors import ToolBudgetExceeded, ToolError
from errand.tools.gateway import Caller
from errand.world.contracts import GetProviderOut, SearchProvidersOut


async def ranked_shortlist(
    deps: RunDeps,
    result: SearchProvidersOut,
    *,
    window: TimeWindow,
    budget: int | Literal["none"] | None,
    brief_version: int,
) -> Shortlist:
    eligible, excluded = views.hard_filter(result, window=window, budget=budget, tz=deps.tz)
    inputs = {
        e.summary.provider_id: ranking.RankInput(
            provider_id=e.summary.provider_id,
            rating=e.summary.rating,
            price_inr=e.summary.price_inr,
            fitting_slots=len(e.fitting),
        )
        for e in eligible
    }
    provisional = ranking.rank(list(inputs.values()), budget=budget)
    left = max(0, deps.reader.settings.calls_per_run - deps.counters.reader_calls)
    reads = await enrich(deps, [r.provider_id for r in provisional[:left]])

    final_inputs = []
    for pid, base in inputs.items():
        read = reads.get(pid)
        final_inputs.append(
            base
            if read is None
            else replace(base, review_ratings=dict(read.ratings), review_flags=read.flags())
        )
    final = ranking.rank(final_inputs, budget=budget)
    by_id = {e.summary.provider_id: e for e in eligible}
    placed = [
        views.Placed(
            eligible=by_id[r.provider_id],
            score=r.score,
            score_parts=r.parts,
            digest=reads[r.provider_id].digest if r.provider_id in reads else None,
        )
        for r in final
    ]
    return views.assemble_shortlist(
        placed, excluded, brief_version=brief_version, window_label=window.label
    )


async def enrich(deps: RunDeps, provider_ids: list[str]) -> dict[str, ReadResult]:
    """Open each provider (in order, so world seqs are deterministic), then read all
    their reviews in one concurrent batch."""
    fetched: list[GetProviderOut] = []
    for pid in provider_ids:
        try:
            answer = await deps.tools.call(Caller.RANKER, "get_provider", {"provider_id": pid})
        except ToolBudgetExceeded:
            break  # out of tool calls or time: rank what we have
        except ToolError:
            continue  # this provider simply gets no digest
        if isinstance(answer, GetProviderOut):
            fetched.append(answer)
    if not fetched:
        return {}
    results = await deps.reader.read_many(fetched)
    return {r.provider_id: r for r in results}
