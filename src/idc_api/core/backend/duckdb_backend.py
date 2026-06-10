"""Read-only DuckDB backend over the bundled idc-index Parquet.

Design notes (see ``dev/api_v3_plan.md`` → Safety):

* DuckDB in-memory tables are *writable*, so a malicious ``DELETE``/``DROP`` from the SQL
  tool could corrupt shared state. We therefore **build a DuckDB file** from the Parquet
  once (external access enabled), then **reopen it ``read_only=True``** — making all data
  immutable for every connection — and apply DuckDB's documented hardening for untrusted
  SQL, finishing with ``lock_configuration=true`` so none of it can be re-enabled.
* DuckDB connections are not thread-safe, so every query runs on a fresh ``cursor()`` of
  the shared read-only connection.
* Statement timeouts are enforced by running the query in a worker thread and calling
  ``cursor.interrupt()`` if it overruns.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import idc_index_data

from .. import schema
from ..errors import InvalidQueryError, QueryTimeoutError
from .base import QueryBackend, QueryResult

# DuckDB hardening for executing untrusted SQL, applied on the read-only serving
# connection. Verbatim from https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview
# lock_configuration MUST be last — it freezes everything above it.
_LEADING_KEYWORD_RE = re.compile(r"^(select|with)\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)  # /* block */
    sql = re.sub(r"--[^\n]*", " ", sql)  # -- line
    return sql


def _has_multiple_statements(sql: str) -> bool:
    """True if there's a ``;`` outside string/identifier quotes that is not the trailing one."""
    in_single = in_double = False
    stripped = sql.rstrip()
    for i, ch in enumerate(stripped):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            if i != len(stripped) - 1:  # a ';' that isn't the final char => 2nd statement
                return True
    return False


def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def build_database_file(path: str) -> None:
    """Build a DuckDB file containing the bundled idc-index tables.

    Used both by ``DuckDBBackend`` on first run and at image-build time (bake the file into
    the container, point ``IDC_API_DUCKDB_PATH`` at it) for instant cold starts.
    """
    con = duckdb.connect(path)
    try:
        for table in schema.list_table_names():
            con.execute(
                f'CREATE TABLE "{table}" AS SELECT * FROM read_parquet(?)',
                [schema.parquet_path(table)],
            )
    finally:
        con.close()


class DuckDBBackend(QueryBackend):
    def __init__(self, settings):
        self._settings = settings
        db_path = self._ensure_database()
        # Harden at connect time via the config dict (not post-connect SET). DuckDB shares one
        # database instance per file path within a process; applying identical config at
        # connect lets multiple backends (e.g. REST + tests) coexist, whereas post-connect
        # SET + lock_configuration would make the 2nd connection fail on the locked instance.
        self._con = duckdb.connect(db_path, read_only=True, config=self._hardening_config())

    # --- construction ---------------------------------------------------------------------
    def _ensure_database(self) -> str:
        """Return a path to a DuckDB file containing the bundled tables, building it if
        necessary. Pinned to the installed ``idc-index-data`` version."""
        if self._settings.duckdb_path:
            return self._settings.duckdb_path

        cache = Path(tempfile.gettempdir()) / f"idc_api_v3_{idc_index_data.__version__}.duckdb"
        if cache.exists():
            return str(cache)

        tmp = cache.with_suffix(f".{os.getpid()}.building")
        if tmp.exists():
            tmp.unlink()
        build_database_file(str(tmp))
        os.replace(tmp, cache)  # atomic publish
        return str(cache)

    def _hardening_config(self) -> dict[str, str]:
        """DuckDB settings for running untrusted SQL (https://duckdb.org/docs/stable/
        operations_manual/securing_duckdb/overview), applied at connect time. With the
        connection also opened read_only, data is immutable and external file/network
        access is denied."""
        s = self._settings
        return {
            "enable_external_access": "false",
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "allow_community_extensions": "false",
            "memory_limit": str(s.duckdb_memory_limit),
            "threads": str(int(s.duckdb_threads)),
            "max_temp_directory_size": str(s.duckdb_temp_directory_size),
            "lock_configuration": "true",  # freeze the above
        }

    # --- execution ------------------------------------------------------------------------
    def _execute(
        self, sql: str, params: list[Any] | None, timeout_s: float | None
    ) -> tuple[list[str], list[tuple]]:
        cur = self._con.cursor()

        def work() -> tuple[list[str], list[tuple]]:
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, cur.fetchall()

        if not timeout_s or timeout_s <= 0:
            return work()

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(work)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeout as exc:
            try:
                cur.interrupt()
            except Exception:
                pass
            raise QueryTimeoutError(
                f"Query exceeded the {timeout_s:g}s statement timeout and was cancelled."
            ) from exc
        finally:
            executor.shutdown(wait=False)

    def _result(self, cols: list[str], rows: list[tuple], max_rows: int | None) -> QueryResult:
        truncated = False
        if max_rows is not None and len(rows) > max_rows:
            rows = rows[:max_rows]
            truncated = True
        dict_rows = [{c: _jsonify(v) for c, v in zip(cols, r)} for r in rows]
        return QueryResult(columns=cols, rows=dict_rows, truncated=truncated, max_rows=max_rows)

    # --- QueryBackend ---------------------------------------------------------------------
    def list_tables(self) -> list[str]:
        return schema.list_table_names()

    def query(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> QueryResult:
        cols, rows = self._execute(sql, params, timeout_s)
        return self._result(cols, rows, max_rows)

    def run_user_sql(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        timeout_s: float | None = None,
    ) -> QueryResult:
        self._validate_select(sql)
        max_rows = max_rows if max_rows is not None else self._settings.sql_max_rows
        timeout_s = timeout_s if timeout_s is not None else self._settings.sql_timeout_seconds
        inner = _strip_sql_comments(sql).strip().rstrip(";").strip()
        # Engine-level row cap: wrap and fetch one extra row to detect truncation. The
        # read-only connection + disabled external access are the real safety boundary.
        wrapped = f"SELECT * FROM (\n{inner}\n) AS _idc_sub LIMIT {max_rows + 1}"
        cols, rows = self._execute(wrapped, None, timeout_s)
        return self._result(cols, rows, max_rows)

    def _validate_select(self, sql: str) -> None:
        cleaned = _strip_sql_comments(sql).strip()
        if not cleaned:
            raise InvalidQueryError("Empty query.")
        if not _LEADING_KEYWORD_RE.match(cleaned):
            raise InvalidQueryError(
                "Only read-only SELECT (or WITH ... SELECT) statements are allowed."
            )
        if _has_multiple_statements(cleaned):
            raise InvalidQueryError("Only a single statement is allowed; remove extra ';'.")

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass
