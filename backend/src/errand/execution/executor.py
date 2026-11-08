"""ConsequentialClient: the only module allowed to call book, cancel or reschedule.

It is the one importer of `errand.tools.consequential` (import-linter), and every call
demands an `ApprovalToken` that `errand.approvals.service.consume` minted after its
guard transaction committed. A token built any other way, or one for a different
intent than the attempt being sent, is refused before anything reaches the world.

The calls run under `asyncio.shield`: once a booking request has been handed to the
provider, cancelling the run (a closed tab, the outer backstop) must not cancel it.
The attempt row was written first, so a run that dies here leaves an `in_flight`
attempt for the reconciler (M6), never an unrecorded booking.
"""

from __future__ import annotations

import asyncio

from errand.approvals.service import ApprovalToken, is_minted
from errand.tools import consequential
from errand.tools.gateway import ToolGateway
from errand.world.contracts import BookError, BookOut


class TokenRefused(PermissionError):
    pass


class ConsequentialClient:
    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def _check(token: ApprovalToken, intent_id: str) -> None:
        if not is_minted(token):
            raise TokenRefused("no ApprovalToken from a committed consume")
        if token.intent_id != intent_id:
            raise TokenRefused(f"the token is for {token.intent_id}, not {intent_id}")

    async def book(
        self, token: ApprovalToken, *, intent_id: str, hold_id: str, idempotency_key: str
    ) -> BookOut | BookError:
        self._check(token, intent_id)
        call = asyncio.ensure_future(
            consequential.book(self._gateway, hold_id=hold_id, idempotency_key=idempotency_key)
        )
        return await asyncio.shield(call)
