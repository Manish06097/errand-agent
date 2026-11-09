"""The fault helper: inert without ERRAND_FAULT, exact with it, strict about typos."""

from __future__ import annotations

import pytest

from errand import faults


@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)

    monkeypatch.setattr(faults.os, "_exit", fake_exit)
    return codes


def test_without_the_variable_nothing_happens(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    monkeypatch.delenv("ERRAND_FAULT", raising=False)
    for point in faults.POINTS:
        faults.fault_point(point)
    assert exits == [] and faults.configure() is None


def test_the_named_point_exits_with_code_1_and_the_others_do_not(
    monkeypatch: pytest.MonkeyPatch, exits: list[int]
) -> None:
    monkeypatch.setenv("ERRAND_FAULT", "after_world_book:exit")
    faults.fault_point("after_intent_write")
    faults.fault_point("after_grant_commit")
    assert exits == []
    faults.fault_point("after_world_book")
    assert exits == [1]
    assert faults.configure() == "after_world_book"


@pytest.mark.parametrize(
    "value", ["after_world_boook:exit", "after_world_book", "after_world_book:sleep", ":exit"]
)
def test_an_unknown_point_or_action_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ERRAND_FAULT", value)
    with pytest.raises(ValueError, match="ERRAND_FAULT"):
        faults.configure()


def test_a_call_site_with_an_unknown_name_is_a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRAND_FAULT", "after_world_book:exit")
    with pytest.raises(ValueError, match="unknown fault point"):
        faults.fault_point("after_somewhere")


def test_only_the_helper_reads_the_variable() -> None:
    """Nothing in the product reads ERRAND_FAULT but errand/faults.py."""
    from pathlib import Path

    src = Path(faults.__file__).parent
    readers = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "ERRAND_FAULT" in p.read_text() and p.name != "faults.py"
    ]
    assert readers == []
