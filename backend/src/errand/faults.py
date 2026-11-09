"""Fault points: kill the process at a named place, for the restart tests (architecture 9.7).

`ERRAND_FAULT=<point>:exit` makes `fault_point(<point>)` call `os._exit(1)` when execution
reaches that point, with no cleanup and no `finally`, the way a crash or `kill -9` ends a
process. Exactly three points exist, each called from one line:

- `after_intent_write`: `execute`, after the guard transaction committed the intent and
  its `prepared` attempt, before anything is sent (scenario S36);
- `after_world_book`: `execute`, right after `book` returned, before the attempt is
  marked `unknown` (S37);
- `after_grant_commit`: the grant route, after the grant transaction committed, before
  the run that would consume it starts (S52).

Nothing else reads `ERRAND_FAULT`. With the variable unset, `fault_point` does nothing
and `configure()` returns None. An unknown point or action is refused at startup, so a
typo can never silently disable a crash test.
"""

from __future__ import annotations

import os
from typing import Final

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

POINTS: Final = ("after_intent_write", "after_world_book", "after_grant_commit")
ACTIONS: Final = ("exit",)
ENV: Final = "ERRAND_FAULT"


class FaultSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERRAND_", extra="ignore")

    fault: str | None = None

    @field_validator("fault")
    @classmethod
    def _known(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        point, sep, action = value.partition(":")
        if not sep or point not in POINTS or action not in ACTIONS:
            raise ValueError(
                f"{ENV}={value!r}: expected <point>:exit with point one of {', '.join(POINTS)}"
            )
        return value

    @property
    def point(self) -> str | None:
        return None if self.fault is None else self.fault.partition(":")[0]


def configure() -> str | None:
    """Validate ERRAND_FAULT (raises on an unknown point); the active point, if any.

    Called once from the API lifespan so a typo fails the start, not the crash test.
    """
    return FaultSettings().point


def fault_point(name: str) -> None:
    """Exit the process now if ERRAND_FAULT names this point. Otherwise do nothing."""
    raw = os.environ.get(ENV)
    if not raw:
        return
    if name not in POINTS:
        raise ValueError(f"unknown fault point {name!r}")
    if raw.partition(":")[0] == name:
        os._exit(1)
