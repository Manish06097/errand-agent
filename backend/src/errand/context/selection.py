"""Which memories reach the model, and why every other one does not (13.2, gate 2 15 and 16).

Pure: rows in, a decision for every row out. Relevance is a gate and the score only
orders:

1. not `active` -> excluded with its status (forgotten, pending_confirmation, superseded,
   rejected);
2. scope is neither global nor the brief's service -> `irrelevant:scope`;
3. the registry says the key does not apply to the errand kind -> `irrelevant:kind`;
4. a service-scoped row of the same key exists -> the global one is `shadowed:scope`;
5. the stored value no longer parses -> `invalid_value`, never sent;
6. the rest are ordered by `3*[service scope] + 2*[pinned] + 1*[explicit] + 0.5*recency`
   with ties to pinned, then registry order, then service before global, then
   `memory_id`, and packed under the cap: while the section is over, the lowest-ranked
   non-pinned row is dropped as `over_budget`. Pinned rows are never dropped here.

The reasons are the closed set of the manifest (13.4).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from errand.memory.registry import (
    PINNED_KEYS,
    PREFILL_FIELD,
    REGISTRY_ORDER,
    RegistryError,
    spec_for,
)
from errand.store.repos.memories import MemoriesRepo, MemoryRow

DAY_MS = 86_400_000
EXCLUSION_REASONS = frozenset(
    {
        "forgotten",
        "pending_confirmation",
        "superseded",
        "rejected",
        "irrelevant:scope",
        "irrelevant:kind",
        "shadowed:scope",
        "over_budget",
        "invalid_value",
    }
)


@dataclass(frozen=True)
class Chosen:
    row: MemoryRow
    value: Any  # parsed, canonical
    score: float
    prefill: str | None

    @property
    def memory_id(self) -> str:
        return self.row.memory_id


@dataclass(frozen=True)
class Selection:
    included: tuple[Chosen, ...]  # registry order, as S4 renders them
    ranked: tuple[Chosen, ...]  # score order, before any drop
    excluded: dict[str, str]  # memory_id -> reason, for every row not included
    tokens: int
    over_cap: bool  # pinned rows alone exceed the cap: the caller sheds or ends the run


def recency_decay(last_used_or_created_ms: int, now_ms: int, half_life_days: float) -> float:
    age_days = max(0.0, (now_ms - last_used_or_created_ms) / DAY_MS)
    return float(0.5 ** (age_days / half_life_days))


def score_of(row: MemoryRow, *, service: str | None, now_ms: int, half_life_days: float) -> float:
    recent = row.last_used_at if row.last_used_at is not None else row.created_at
    return (
        3.0 * (row.scope == f"service:{service}" if service else 0)
        + 2.0 * (row.key in PINNED_KEYS)
        + 1.0 * (row.kind == "explicit")
        + 0.5 * recency_decay(recent, now_ms, half_life_days)
    )


def select(
    rows: Sequence[MemoryRow],
    *,
    errand_kind: str,
    service: str | None,
    now_ms: int,
    half_life_days: float,
    cap_tokens: int,
    measure: Callable[[Sequence[Chosen]], int],
) -> Selection:
    excluded: dict[str, str] = {}
    candidates: list[tuple[MemoryRow, Any]] = []
    for row in rows:
        if row.status != "active":
            excluded[row.memory_id] = row.status
            continue
        if row.scope != "global" and row.scope != f"service:{service}":
            excluded[row.memory_id] = "irrelevant:scope"
            continue
        try:
            spec = spec_for(row.key)
        except RegistryError:
            excluded[row.memory_id] = "invalid_value"
            continue
        if errand_kind not in spec.kinds:
            excluded[row.memory_id] = "irrelevant:kind"
            continue
        candidates.append((row, row.value))

    service_keys = {r.key for r, _ in candidates if r.scope != "global"}
    live: list[Chosen] = []
    for row, raw in candidates:
        if row.scope == "global" and row.key in service_keys:
            excluded[row.memory_id] = "shadowed:scope"
            continue
        try:
            value = spec_for(row.key).parse(raw)
        except (RegistryError, TypeError, ValueError, KeyError):
            excluded[row.memory_id] = "invalid_value"
            continue
        score = round(
            score_of(row, service=service, now_ms=now_ms, half_life_days=half_life_days), 6
        )
        live.append(Chosen(row, value, score, PREFILL_FIELD.get(row.key)))

    ranked = tuple(sorted(live, key=_rank))
    kept = list(ranked)
    while kept and measure(_registry_order(kept)) > cap_tokens:
        droppable = [c for c in kept if c.row.key not in PINNED_KEYS]
        if not droppable:
            break
        worst = droppable[-1]  # `kept` is in rank order, so the last one is the lowest
        kept.remove(worst)
        excluded[worst.memory_id] = "over_budget"
    included = tuple(_registry_order(kept))
    tokens = measure(included) if included else 0
    return Selection(included, ranked, dict(sorted(excluded.items())), tokens, tokens > cap_tokens)


def _rank(c: Chosen) -> tuple[float, bool, int, bool, str]:
    return (
        -c.score,
        c.row.key not in PINNED_KEYS,
        REGISTRY_ORDER.get(c.row.key, 99),
        c.row.scope == "global",
        c.row.memory_id,
    )


def _registry_order(chosen: Sequence[Chosen]) -> list[Chosen]:
    return sorted(
        chosen, key=lambda c: (REGISTRY_ORDER.get(c.row.key, 99), c.row.scope, c.memory_id)
    )


async def read_rows(repo: MemoriesRepo, user_id: str) -> list[MemoryRow]:
    """Every row of the user: the selection needs them all to give each a reason."""
    return await repo.for_user(user_id)
