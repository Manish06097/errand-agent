"""What users call the services and areas we cover.

One table for product code (value-bound evidence, out-of-coverage) and for the scripted
reference planner in the evals, which imports it from here instead of keeping a copy.
Aliases are lower case; matching is on normalised text.
"""

from __future__ import annotations

from errand.world.contracts import Area, Service

SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "plumber": ("plumber", "plumbing"),
    "electrician": ("electrician", "electrical"),
    "ac_servicing": ("ac servicing", "ac service", "air conditioner", "aircon"),
}
AREA_ALIASES: dict[str, tuple[str, ...]] = {
    "indiranagar": ("indiranagar", "indira nagar"),
    "koramangala": ("koramangala",),
    "whitefield": ("whitefield", "white field"),
    "jayanagar": ("jayanagar", "jaya nagar"),
}

assert set(SERVICE_ALIASES) == {s.value for s in Service}
assert set(AREA_ALIASES) == {a.value for a in Area}


def aliases_of(field: str, value: str) -> tuple[str, ...]:
    """The aliases of one enum value ("area", "indiranagar")."""
    table = AREA_ALIASES if field == "area" else SERVICE_ALIASES
    return table.get(value, ())


def covered_value(field: str, text: str) -> str | None:
    """The covered enum value whose alias appears in `text` (already normalised), if any."""
    table = AREA_ALIASES if field == "area" else SERVICE_ALIASES
    for value, names in table.items():
        if any(name in text for name in names):
            return value
    return None
