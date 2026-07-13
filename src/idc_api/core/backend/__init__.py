"""Query backends. DuckDBBackend is the only implementation, behind the QueryBackend
interface."""

from .base import QueryBackend, QueryResult

__all__ = ["QueryBackend", "QueryResult"]
