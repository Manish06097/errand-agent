"""Every covered service and area has aliases, and no alias table has a stranger."""

from __future__ import annotations

from errand.domain.aliases import AREA_ALIASES, SERVICE_ALIASES
from errand.world.contracts import Area, Service


def test_the_alias_tables_cover_exactly_the_enums() -> None:
    assert set(SERVICE_ALIASES) == {s.value for s in Service}
    assert set(AREA_ALIASES) == {a.value for a in Area}
    assert all(names for names in SERVICE_ALIASES.values())
    assert all(names for names in AREA_ALIASES.values())
