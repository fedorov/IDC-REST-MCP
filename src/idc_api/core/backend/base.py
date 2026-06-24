"""The backend interface that decouples services from the storage/query engine.

Services depend only on ``QueryBackend``. The MVP implementation is DuckDB over the local
idc-index Parquet; a future ``BigQueryBackend`` implements the same surface to reach full
DICOM metadata / SR measurements without changing any service or adapter code.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """Tabular result: ordered column names + row dicts, plus truncation metadata."""

    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool = False
    max_rows: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)


class QueryBackend(abc.ABC):
    """Minimal capability surface the services need from a data backend."""

    @abc.abstractmethod
    def list_tables(self) -> list[str]:
        """Names of queryable tables."""

    def list_clinical_tables(self) -> list[str]:  # pragma: no cover - trivial default
        """Names of per-collection clinical data tables (in the backend's ``clinical``
        namespace), or ``[]`` if this build/backend has no clinical data."""
        return []

    @abc.abstractmethod
    def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> QueryResult:
        """Run a **trusted, parameterized** statement (authored by our services).

        ``params`` are bound via the engine's prepared-statement mechanism (DuckDB ``?``),
        never string-interpolated — this is the OWASP-recommended primary defense for the
        values we control.
        """

    @abc.abstractmethod
    def run_user_sql(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> QueryResult:
        """Run an **untrusted** caller/LLM-supplied statement under the read-only sandbox.

        Implementations must reject anything other than a single read-only SELECT/WITH and
        enforce a row cap + statement timeout.
        """

    def close(self) -> None:  # pragma: no cover - trivial default
        pass
