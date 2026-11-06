"""Tool error taxonomy.

World business answers (`slot_taken`, `hold_expired`, `slot_conflict`, `not_found`,
`invalid_args`, `idempotency_key_reuse`) are not errors: transports return them as
typed results. Exceptions are for what the provider did not answer:

- `ToolTimeout`: no answer within the timeout from `tools/timeouts.py`. For a
  consequential call this says nothing about whether it applied.
- `ToolTransient`: a 503 or other 5xx, or the connection failed. Retryable.
- `ToolUnexpected`: an answer that fails strict parsing (bad JSON, extra fields,
  wrong types, an error code the tool does not have). The raw payload is attached.

`ToolInvalid` and `ToolDefinitive` complete the taxonomy for the ToolGateway: a request
it refused to send, and a business answer it chose to surface as an exception.
"""

from __future__ import annotations


class ToolError(Exception):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool


class ToolTimeout(ToolError):
    def __init__(self, tool: str, timeout_s: float) -> None:
        super().__init__(tool, f"no answer within {timeout_s:g}s")
        self.timeout_s = timeout_s


class ToolTransient(ToolError):
    retryable = True

    def __init__(self, tool: str, status: int | None, body: bytes, detail: str = "") -> None:
        what = f"HTTP {status}" if status is not None else f"connection failed ({detail})"
        super().__init__(tool, f"transient failure: {what}")
        self.status = status
        self.body = body


class ToolUnexpected(ToolError):
    def __init__(self, tool: str, raw: bytes, reason: str, status: int | None = None) -> None:
        super().__init__(tool, f"unexpected response: {reason}")
        self.raw = raw
        self.reason = reason
        self.status = status


class ToolInvalid(ToolError):
    """The gateway refused to send a request (bad arguments or a disallowed caller)."""


class ToolDefinitive(ToolError):
    """A definitive business answer the gateway surfaces as an exception."""

    def __init__(self, tool: str, error: str) -> None:
        super().__init__(tool, error)
        self.error = error
