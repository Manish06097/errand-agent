"""The consequential surface: `book`, `cancel` and `reschedule`, and nothing else.

Only `errand.execution.executor` may import this module (an import-linter contract,
plus `tests/tools/test_consequential_boundary.py`). The ToolGateway refuses these three
tools on its ordinary path for every caller, and on this path for every caller except
`Caller.EXECUTOR`. The executor's `ConsequentialClient` wraps these functions and
demands an `ApprovalToken` minted by a committed consume; that check lives there, so
this module stays a thin, typed door with one key.
"""

from __future__ import annotations

from errand.tools.gateway import Caller, ToolGateway
from errand.world.contracts import (
    BookError,
    BookOut,
    CancelError,
    CancelOut,
    RescheduleError,
    RescheduleOut,
    Slot,
)


async def book(gateway: ToolGateway, *, hold_id: str, idempotency_key: str) -> BookOut | BookError:
    result = await gateway._consequential(
        Caller.EXECUTOR, "book", {"hold_id": hold_id, "idempotency_key": idempotency_key}
    )
    assert isinstance(result, BookOut | BookError)
    return result


async def cancel(
    gateway: ToolGateway, *, booking_id: str, idempotency_key: str, attempt: int = 1
) -> CancelOut | CancelError:
    result = await gateway._consequential(
        Caller.EXECUTOR,
        "cancel",
        {"booking_id": booking_id, "idempotency_key": idempotency_key},
        attempt=attempt,
    )
    assert isinstance(result, CancelOut | CancelError)
    return result


async def reschedule(
    gateway: ToolGateway, *, booking_id: str, slot: Slot, idempotency_key: str, attempt: int = 1
) -> RescheduleOut | RescheduleError:
    result = await gateway._consequential(
        Caller.EXECUTOR,
        "reschedule",
        {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key},
        attempt=attempt,
    )
    assert isinstance(result, RescheduleOut | RescheduleError)
    return result
