"""Query backends. The MVP ships DuckDBBackend; a BigQueryBackend can be added later
behind the same QueryBackend interface without touching services or adapters."""

from .base import QueryBackend, QueryResult

__all__ = ["QueryBackend", "QueryResult"]
