"""`POST /api/dev/reconciler/tick`: run the reconciler's re-check now (ERRAND_DEV=1 only).

The dev drawer's "Reconcile now" and the evals' `reconciler_tick` use it. It runs the same
pass the periodic task runs, for every conversation, and answers once the recovery runs it
started have ended, so the caller can read the result straight away.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request


def register(app: FastAPI) -> None:
    @app.post("/api/dev/reconciler/tick")
    async def reconcile_now(request: Request) -> dict[str, Any]:
        report = await request.app.state.reconciler.tick(wait=True)
        return {
            "recovery_runs": report.recovery_runs,
            "skipped_busy": report.skipped_busy,
        }
