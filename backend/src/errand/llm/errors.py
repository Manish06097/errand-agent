"""Typed failures of the model gateway."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ContextOverBudget(Exception):
    """The next request would exceed the context budget, so nothing was sent."""

    def __init__(self, *, needed: int, est_request: int, floor: int, budget: int) -> None:
        self.needed = needed
        self.est_request = est_request
        self.floor = floor
        self.budget = budget
        super().__init__(
            f"request needs {needed} tokens (estimate {est_request}, in-loop floor {floor}), "
            f"over the budget of {budget}; nothing was sent"
        )


class ContextStale(Exception):
    """The memory epoch moved since the request was built; nothing was sent (11)."""

    def __init__(self, *, built: int, current: int) -> None:
        self.built = built
        self.current = current
        super().__init__(
            f"the request was built at memory epoch {built} but the epoch is {current}; "
            "nothing was sent"
        )


class ModelCallLimit(Exception):
    """The run already made its maximum number of model calls; nothing was sent."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(f"the run already made {cap} model calls; nothing was sent")


class ModelUnavailable(Exception):
    """The planner model could not be reached after the SDK's own retries (429, 5xx
    including 529, a connection error or a timeout with run time left). Nothing from the
    failed request reached the run; `plan` renders MODEL_UNAVAILABLE."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"the planning model is unavailable: {reason}")


class ModelDeadline(Exception):
    """A planner call hit the run's wall-clock deadline (the call timeout was capped by
    the time left), so `plan` ends the run Limit(deadline), not MODEL_UNAVAILABLE."""

    def __init__(self) -> None:
        super().__init__("the run deadline passed during a planner call")


class ForcedToolChoiceError(ValueError):
    """Forced tool_choice is a 400 on Opus 5.5 and LangChain silently drops it."""


class ScriptExhausted(RuntimeError):
    """A list-driven ScriptedChatModel was asked for more responses than it holds."""


class CassetteMiss(LookupError):
    """Replay found no recording for a request."""


class LoopPrefixViolation(AssertionError):
    """The loop history changed between calls, which would invalidate thinking signatures."""


class MissingUsage(RuntimeError):
    """A response carried no usage_metadata, so it cannot be priced or budgeted."""


def forced_choice_error(tool_choice: str | Mapping[str, Any]) -> ForcedToolChoiceError:
    return ForcedToolChoiceError(
        f"forced tool_choice {tool_choice!r} is not allowed: claude-opus-5-5 rejects it with "
        "a 400, and LangChain drops it silently when thinking is on. Steer with the prompt "
        "and tool descriptions instead (architecture 10.4)."
    )


def check_tool_choice(tool_choice: str | Mapping[str, Any] | None) -> None:
    """Accept only "auto", {"type": "auto"} or None; anything else forces a tool."""
    if tool_choice is None or tool_choice == "auto":
        return
    if isinstance(tool_choice, Mapping) and tool_choice.get("type") == "auto":
        extra = set(tool_choice) - {"type", "disable_parallel_tool_use"}
        if not extra:
            return
    raise forced_choice_error(tool_choice)
