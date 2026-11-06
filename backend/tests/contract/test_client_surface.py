"""WorldClient is the eight tools and nothing else; inspection lives elsewhere."""

from __future__ import annotations

from errand.tools.client import HttpTransport, InProcessTransport, WorldClient
from errand.tools.inspector import HttpInspector, InProcessInspector
from errand.world.contracts import TOOLS


def public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_world_client_is_exactly_the_eight_tools() -> None:
    assert public(WorldClient) == set(TOOLS)
    assert public(InProcessTransport) == set(TOOLS)
    assert public(HttpTransport) == set(TOOLS)


def test_inspector_shares_nothing_with_the_client() -> None:
    for inspector in (InProcessInspector, HttpInspector):
        assert not public(inspector) & set(TOOLS)
