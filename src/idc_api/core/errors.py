"""Typed errors shared by both adapters.

Each error carries a stable machine ``code`` and an HTTP ``status`` so the REST adapter can
map it to a response and the MCP adapter can return a structured ``is_error`` payload. We
never leak stack traces to callers.
"""

from __future__ import annotations


class IDCAPIError(Exception):
    """Base class for all expected, caller-facing errors."""

    code: str = "idc_api_error"
    status: int = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(IDCAPIError):
    code = "not_found"
    status = 404


class InvalidQueryError(IDCAPIError):
    """The SQL submitted to the guarded query tool is not an allowed read-only statement."""

    code = "invalid_query"
    status = 400


class QueryTimeoutError(IDCAPIError):
    """A query exceeded the configured statement timeout and was interrupted."""

    code = "query_timeout"
    status = 504


class ResultTooLargeError(IDCAPIError):
    code = "result_too_large"
    status = 400
