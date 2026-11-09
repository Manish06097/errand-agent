"""The memory registry: one entry per key, as code (gate 2 decision 2).

Everything that varies by key lives here: which scopes it takes, whether it is pinned,
which errand kinds it applies to, how a raw value is parsed into its stored form, how
the stored form is rendered back for S4, and the surface forms a forget has to find
("10:00", "10 am", "ten" for a time). An unknown key or a badly typed value is a
`RegistryError`, which the tools turn into a tool error.

Stored values are canonical JSON-able Python: an area is its enum value, a time is
"HH:MM", weekdays are lowercase names, money is an int, a provider is its id. A stored
row that no longer parses is skipped as `invalid_value` instead of being sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import Any, Literal

from errand.tools.sanitize import clean_text
from errand.world.contracts import Area

KINDS_ALL = frozenset({"book", "cancel", "reschedule", "rebook", "follow-up"})
KINDS_BOOKING = frozenset({"book", "rebook"})
KINDS_WINDOW = frozenset({"book", "reschedule", "rebook"})
GLOBAL = "global"
ACCESS_NOTE_CAP = 120

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LANGUAGES = ("english", "hindi", "hinglish", "kannada")

# Aliases for each area, lowercase. M8 owns the full alias table for the brief; memory
# keeps the spellings a forget has to find, so it needs no dependency on that module.
AREA_ALIASES: dict[str, tuple[str, ...]] = {
    "indiranagar": ("indiranagar", "indira nagar", "indiranager", "indranagar"),
    "koramangala": ("koramangala", "kormangala", "koramangla", "koramanagala"),
    "whitefield": ("whitefield", "white field"),
    "jayanagar": ("jayanagar", "jaya nagar", "jayanager"),
}
_PROVIDER = re.compile(r"^[PEA][1-9][0-9]{0,2}$")
# Provider ids start with the letter of their service (world/catalog.py service_of).
SERVICE_LETTER = {"plumber": "P", "electrician": "E", "ac_servicing": "A"}
_SERVICE_SCOPE = re.compile(r"^service:[a-z][a-z_]{1,30}$")
_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


class RegistryError(ValueError):
    """An unknown key, a scope the key does not take, or a value of the wrong type."""


ScopeKind = Literal["global", "service"]


@dataclass(frozen=True)
class KeySpec:
    key: str
    description: str
    scopes: frozenset[ScopeKind]
    pinned: bool
    feeds: str
    kinds: frozenset[str]
    parse: Callable[[Any], Any]
    render: Callable[[Any], str]
    forms: Callable[[Any], tuple[str, ...]]
    inferred_by: str | None = None
    # The key this one contradicts within one scope, if any.
    contradicts: str | None = None

    def canonical(self, raw: Any) -> Any:
        return self.parse(raw)


# ----------------------------------------------------------------------------- parsers


def _parse_area(raw: Any) -> str:
    text = str(raw).strip().lower()
    for area, aliases in AREA_ALIASES.items():
        if text in aliases:
            return Area(area).value
    raise RegistryError(f"home_area must be one of {', '.join(a.value for a in Area)}, got {raw!r}")


def parse_clock(raw: Any) -> str:
    """ "10:00", "10", "10am" and a `time` all become "HH:MM"."""
    if isinstance(raw, time):
        return f"{raw.hour:02d}:{raw.minute:02d}"
    text = str(raw).strip().lower().replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if match is None:
        raise RegistryError(f"not a time of day: {raw!r}")
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RegistryError(f"not a time of day: {raw!r}")
    return f"{hour:02d}:{minute:02d}"


def _parse_weekdays(raw: Any) -> list[str]:
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    days: set[str] = set()
    for item in items:
        text = str(item).strip().lower()
        match = [d for d in WEEKDAYS if d == text or d[:3] == text]
        if len(match) != 1:
            raise RegistryError(f"not a weekday: {item!r}")
        days.add(match[0])
    if not days:
        raise RegistryError("avoid_days needs at least one weekday")
    return [d for d in WEEKDAYS if d in days]


def _parse_inr(raw: Any) -> int:
    if isinstance(raw, bool):
        raise RegistryError(f"not an amount in INR: {raw!r}")
    if isinstance(raw, int):
        value = raw
    else:
        digits = re.sub(r"[₹,\s]|rs\.?|inr", "", str(raw).lower())
        if not digits.isdigit():
            raise RegistryError(f"not an amount in INR: {raw!r}")
        value = int(digits)
    if not 100 <= value <= 100_000:
        raise RegistryError(f"budget_ceiling must be between 100 and 100000 INR, got {value}")
    return value


def _parse_provider(raw: Any) -> str:
    text = str(raw).strip().upper()
    if not _PROVIDER.match(text):
        raise RegistryError(f"not a provider id: {raw!r}")
    return text


def _parse_providers(raw: Any) -> list[str]:
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    ids = sorted({_parse_provider(i) for i in items}, key=lambda p: int(p[1:]))
    if not ids or len(ids) > 5:
        raise RegistryError("avoid_provider takes between 1 and 5 provider ids")
    return ids


def _parse_language(raw: Any) -> str:
    text = str(raw).strip().lower()
    if text not in LANGUAGES:
        raise RegistryError(f"language must be one of {', '.join(LANGUAGES)}, got {raw!r}")
    return text


def _parse_note(raw: Any) -> str:
    text = clean_text(str(raw), limit=ACCESS_NOTE_CAP)
    if len(text) < 4:
        raise RegistryError("access_note needs at least 4 characters")
    return text


def _parse_contact_window(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"from", "to"}:
        raise RegistryError("contact_window is {from: HH:MM, to: HH:MM}")
    start, end = parse_clock(raw["from"]), parse_clock(raw["to"])
    if start >= end:
        raise RegistryError("contact_window must start before it ends")
    return {"from": start, "to": end}


# ---------------------------------------------------------------------------- renderers


def _clock_forms(value: str) -> tuple[str, ...]:
    hour, minute = int(value[:2]), int(value[3:])
    h12 = hour % 12 or 12
    meridiem = "am" if hour < 12 else "pm"
    forms = [value, f"{hour}:{minute:02d}"]
    if minute == 0:
        forms += [
            f"{h12} {meridiem}",
            f"{h12}{meridiem}",
            f"{h12} {meridiem[0]}.m.",
            f"{h12}:00 {meridiem}",
            f"{h12} o'clock",
        ]
        if h12 in _NUMBER_WORDS:
            forms.append(_NUMBER_WORDS[h12])
    else:
        forms += [f"{h12}:{minute:02d} {meridiem}", f"{h12}:{minute:02d}{meridiem}"]
    return tuple(dict.fromkeys(forms))


def _clock_render(value: str) -> str:
    return value


def _money_forms(value: int) -> tuple[str, ...]:
    plain, grouped = str(value), f"{value:,}"
    forms = [f"₹{grouped}", f"₹{plain}", f"rs {plain}", f"rs. {plain}", f"{plain} rupees"]
    forms += [f"rs {grouped}", f"inr {plain}", plain, grouped]
    return tuple(dict.fromkeys(forms))


def _list_render(value: list[str]) -> str:
    return ", ".join(value)


def _area_forms(value: str) -> tuple[str, ...]:
    return AREA_ALIASES.get(value, (value,))


def _days_forms(value: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*value, *(d + "s" for d in value), *(d[:3] for d in value)]))


def _window_forms(value: dict[str, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*_clock_forms(value["from"]), *_clock_forms(value["to"])]))


_REGISTRY_LIST: tuple[KeySpec, ...] = (
    KeySpec(
        "home_area",
        "the area the user lives in",
        frozenset({"global"}),
        True,
        "brief area",
        KINDS_BOOKING,
        _parse_area,
        str,
        _area_forms,
    ),
    KeySpec(
        "earliest_time",
        "never book before this time of day",
        frozenset({"global", "service"}),
        True,
        "window not_before",
        KINDS_WINDOW,
        parse_clock,
        _clock_render,
        _clock_forms,
        contradicts="latest_time",
    ),
    KeySpec(
        "latest_time",
        "never book after this time of day",
        frozenset({"global", "service"}),
        True,
        "window not_after",
        KINDS_WINDOW,
        parse_clock,
        _clock_render,
        _clock_forms,
        inferred_by="latest_time_before_noon",
        contradicts="earliest_time",
    ),
    KeySpec(
        "avoid_days",
        "weekdays to avoid",
        frozenset({"global", "service"}),
        False,
        "window",
        KINDS_WINDOW,
        _parse_weekdays,
        _list_render,
        _days_forms,
    ),
    KeySpec(
        "budget_ceiling",
        "usual maximum spend in INR",
        frozenset({"service"}),
        False,
        "brief budget_max_inr",
        KINDS_BOOKING,
        _parse_inr,
        lambda v: f"₹{v}",
        _money_forms,
    ),
    KeySpec(
        "preferred_provider",
        "provider the user likes",
        frozenset({"service"}),
        False,
        "ranking pref_match",
        KINDS_BOOKING,
        _parse_provider,
        str,
        lambda v: (v,),
        inferred_by="same_provider_twice",
        contradicts="avoid_provider",
    ),
    KeySpec(
        "avoid_provider",
        "providers the user never wants",
        frozenset({"service"}),
        False,
        "hard filter",
        KINDS_BOOKING,
        _parse_providers,
        _list_render,
        lambda v: tuple(v),
        contradicts="preferred_provider",
    ),
    KeySpec(
        "language",
        "reply language",
        frozenset({"global"}),
        False,
        "reply language",
        KINDS_ALL,
        _parse_language,
        str,
        lambda v: (v,),
    ),
    KeySpec(
        "access_note",
        "how a provider gets in (free text, data only)",
        frozenset({"global"}),
        False,
        "context only",
        KINDS_BOOKING,
        _parse_note,
        str,
        lambda v: (v,),
    ),
    KeySpec(
        "contact_window",
        "when it is fine to contact the user",
        frozenset({"global"}),
        False,
        "follow-up notify policy",
        frozenset({"follow-up"}),
        _parse_contact_window,
        lambda v: f"{v['from']}-{v['to']}",
        _window_forms,
    ),
)

REGISTRY: dict[str, KeySpec] = {spec.key: spec for spec in _REGISTRY_LIST}
REGISTRY_ORDER: dict[str, int] = {spec.key: i for i, spec in enumerate(_REGISTRY_LIST)}
PINNED_KEYS = frozenset(k for k, s in REGISTRY.items() if s.pinned)


def spec_for(key: str) -> KeySpec:
    spec = REGISTRY.get(key)
    if spec is None:
        raise RegistryError(f"unknown memory key {key!r}; known: {', '.join(REGISTRY)}")
    return spec


def check_scope(spec: KeySpec, scope: str) -> str:
    if scope == GLOBAL:
        if "global" not in spec.scopes:
            raise RegistryError(f"{spec.key} cannot be global; it is per service")
        return scope
    if _SERVICE_SCOPE.match(scope):
        if "service" not in spec.scopes:
            raise RegistryError(f"{spec.key} is global only")
        return scope
    raise RegistryError(f"scope must be 'global' or 'service:<name>', got {scope!r}")


def parse_value(key: str, scope: str, raw: Any) -> tuple[KeySpec, str, Any]:
    spec = spec_for(key)
    scope = check_scope(spec, scope)
    value = spec.parse(raw)
    if key in ("preferred_provider", "avoid_provider"):
        letter = SERVICE_LETTER.get(service_of_scope(scope) or "")
        ids = [value] if isinstance(value, str) else value
        if letter is None or any(not i.startswith(letter) for i in ids):
            raise RegistryError(
                f"{key} in {scope} takes {letter or 'no'} provider ids, got {value}"
            )
    return spec, scope, value


def dump_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_value(key: str, value: Any) -> str:
    """The stored value as S4 and the UI show it; raises RegistryError if it no longer parses."""
    spec = spec_for(key)
    return spec.render(spec.parse(value))


def surface_forms(key: str, value: Any) -> tuple[str, ...]:
    """Every spelling of a value a forget must find in the user's and the model's text."""
    spec = spec_for(key)
    return spec.forms(spec.parse(value))


def service_of_scope(scope: str) -> str | None:
    return scope.split(":", 1)[1] if scope.startswith("service:") else None


def _providers(value: Any) -> set[str]:
    return {value} if isinstance(value, str) else set(value or [])


def conflicts(key: str, value: Any, other_key: str, other_value: Any) -> bool:
    """Do two stored values in one scope contradict (gate 2 decision 2)?

    Preferring and avoiding a shared provider, or an earliest_time at or after the
    latest_time. Anything else never conflicts, so a pending "latest 12:00" is not
    rejected by an unrelated "earliest 09:00".
    """
    if {key, other_key} == {"preferred_provider", "avoid_provider"}:
        return bool(_providers(value) & _providers(other_value))
    if {key, other_key} == {"earliest_time", "latest_time"}:
        earliest, latest = (value, other_value) if key == "earliest_time" else (other_value, value)
        return bool(earliest >= latest)
    return False


# The brief field a key fills ahead of the user's answer (gate 2 decision 17). The window
# keys feed M8's not_before / not_after, which are not a brief field of their own.
PREFILL_FIELD: dict[str, str] = {"home_area": "area", "budget_ceiling": "budget_max_inr"}
