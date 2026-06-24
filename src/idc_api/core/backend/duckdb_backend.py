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

# Schema name the per-collection clinical data tables are registered under (defined in
# ``schema`` as the naming source of truth), keeping the main catalog (``list_tables``)
# index-focused while ``run_sql`` can still reach ``clinical.<table>``.
CLINICAL_SCHEMA = schema.CLINICAL_SCHEMA

# Bumped when the *shape* of the built DuckDB file changes in a way the data-version + include
# token don't capture (e.g. adding the clinical schema). Folded into the cache filename so a
# stale file built by older code is not reused. Bump on any such structural change.
_BUILD_REVISION = 2


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


def _fetch_specialized_parquets(names: list[str]) -> tuple[dict[str, str], str | None]:
    """Download (via idc-index) the Parquet for each specialized table, returning
    ``({table_name: local_parquet_path}, clinical_dir)``. Requires network; raises a clear
    error on failure.

    ``clinical_dir`` is the local directory of per-collection clinical data tables that
    idc-index downloads as a side effect of ``fetch_index('clinical_index')`` (or ``None`` if
    ``clinical_index`` was not requested). It is registered separately as the ``clinical``
    schema by ``_register_clinical_tables``.

    idc-index is imported lazily and a single client is reused for all fetches (instantiating
    it loads the main index once), so the serving path never pays for the heavier client.
    """
    if not names:
        return {}, None
    try:
        from idc_index import IDCClient
    except Exception as exc:  # pragma: no cover - idc-index is a hard dependency
        raise RuntimeError("idc-index is required to fetch specialized indices.") from exc

    client = IDCClient()
    paths: dict[str, str] = {}
    for name in names:
        try:
            client.fetch_index(name)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch specialized index {name!r} (network is required at build "
                f"time). To build offline with bundled tables only, set "
                f"IDC_API_INCLUDE_INDICES=none. Cause: {exc}"
            ) from exc
        fp = client.indices_overview.get(name, {}).get("file_path")
        if not fp or not Path(fp).exists():
            raise RuntimeError(
                f"Specialized index {name!r} did not resolve to a local Parquet file after "
                f"fetch; cannot include it."
            )
        paths[name] = fp
    # fetch_index('clinical_index') also downloads the per-collection clinical data tables into
    # client.clinical_data_dir; surface it so build_database_file can register them.
    clinical_dir = None
    if "clinical_index" in names:
        cd = getattr(client, "clinical_data_dir", None)
        if cd and Path(cd).is_dir():
            clinical_dir = str(cd)
    return paths, clinical_dir


def build_database_file(path: str, specialized: list[str] | None = None) -> None:
    """Build a DuckDB file containing the bundled idc-index tables plus the requested
    ``specialized`` indices (``None`` -> all specialized indices).

    Bundled tables come from the Parquet shipped in ``idc-index-data`` (offline). Specialized
    indices are fetched over the network here at build time. Used both by ``DuckDBBackend`` on
    first run and at image-build time (bake the file into the container, point
    ``IDC_API_DUCKDB_PATH`` at it) for instant cold starts.
    """
    if specialized is None:
        specialized = schema.specialized_table_names()
    fetched, clinical_dir = _fetch_specialized_parquets(specialized)

    con = duckdb.connect(path)
    try:
        for table in schema.bundled_table_names():
            con.execute(
                f'CREATE TABLE "{table}" AS SELECT * FROM read_parquet(?)',
                [schema.parquet_path(table)],
            )
        for table, parquet in fetched.items():
            con.execute(
                f'CREATE TABLE "{table}" AS SELECT * FROM read_parquet(?)',
                [parquet],
            )
        if clinical_dir:
            _register_clinical_tables(con, clinical_dir)
    finally:
        con.close()


# A clinical table name is a directory under clinical_data_dir; these come from IDC, but we
# still constrain them to a safe identifier shape before interpolating into DDL (we cannot bind
# an identifier as a parameter) — anything else is skipped rather than trusted.
_CLINICAL_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _register_clinical_tables(con: "duckdb.DuckDBPyConnection", clinical_dir: str) -> None:
    """Register each per-collection clinical data table under the ``clinical`` schema.

    idc-index lays the tables out as ``<clinical_dir>/<short_table_name>/*.parquet``. We
    materialize each into ``clinical."<name>"`` via CREATE TABLE AS SELECT (copying the rows
    into the DuckDB file, like the specialized tables), so the read-only serving connection
    never needs the Parquet directory. These tables join to the main ``index`` on
    ``dicom_patient_id = index.PatientID``.
    """
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{CLINICAL_SCHEMA}"')
    for entry in sorted(Path(clinical_dir).iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if not _CLINICAL_TABLE_NAME_RE.match(name):
            continue
        if not any(entry.glob("*.parquet")):
            continue
        glob = str(entry / "*.parquet")
        con.execute(
            f'CREATE TABLE "{CLINICAL_SCHEMA}"."{name}" AS SELECT * FROM read_parquet(?)',
            [glob],
        )


class DuckDBBackend(QueryBackend):
    def __init__(self, settings):
        self._settings = settings
        db_path = self._ensure_database()
        # Harden at connect time via the config dict (not post-connect SET). DuckDB shares one
        # database instance per file path within a process; applying identical config at
        # connect lets multiple backends (e.g. REST + tests) coexist, whereas post-connect
        # SET + lock_configuration would make the 2nd connection fail on the locked instance.
        self._con = duckdb.connect(db_path, read_only=True, config=self._hardening_config())
        # Reflect exactly what was built into this file (bundled + whichever specialized
        # indices were included), in registry order. Computed once — tables never change.
        self._table_names = self._catalog_table_names()
        # Per-collection clinical data tables live in the ``clinical`` schema (present only when
        # clinical_index was included). Kept separate from _table_names so list_tables stays
        # index-focused; surfaced via list_clinical_tables for the clinical discovery tools.
        self._clinical_table_names = self._catalog_clinical_table_names()

    # --- construction ---------------------------------------------------------------------
    def _ensure_database(self) -> str:
        """Return a path to a DuckDB file containing the bundled tables (plus any requested
        specialized indices), building it if necessary. Pinned to the installed
        ``idc-index-data`` version, and keyed by which specialized indices are included so
        different ``IDC_API_INCLUDE_INDICES`` settings don't collide on one cache file."""
        if self._settings.duckdb_path:
            return self._settings.duckdb_path

        included = schema.resolve_include(self._settings.include_indices)
        token = schema.include_token(included)
        cache = (
            Path(tempfile.gettempdir())
            / f"idc_api_v3_{idc_index_data.__version__}_{token}_r{_BUILD_REVISION}.duckdb"
        )
        if cache.exists():
            return str(cache)

        tmp = cache.with_suffix(f".{os.getpid()}.building")
        if tmp.exists():
            tmp.unlink()
        build_database_file(str(tmp), included)
        os.replace(tmp, cache)  # atomic publish
        return str(cache)

    def _catalog_table_names(self) -> list[str]:
        """Tables actually present in the open database, ordered by the schema registry
        (bundled first, then specialized), so the listing reflects this build exactly."""
        cur = self._con.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        )
        present = {r[0] for r in cur.fetchall()}
        ordered = [t for t in schema.registered_table_names() if t in present]
        ordered += sorted(present - set(ordered))  # any unexpected tables, deterministically
        return ordered

    def _catalog_clinical_table_names(self) -> list[str]:
        """Per-collection clinical data tables present in the ``clinical`` schema, sorted.
        Empty when clinical_index wasn't included in this build."""
        cur = self._con.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [CLINICAL_SCHEMA],
        )
        return sorted(r[0] for r in cur.fetchall())

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
        return list(self._table_names)

    def list_clinical_tables(self) -> list[str]:
        return list(self._clinical_table_names)

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
        try:
            cols, rows = self._execute(wrapped, None, timeout_s)
        except duckdb.Error as exc:
            # Surface the engine's message verbatim — its "Did you mean ...?" / candidate-
            # binding hints are what let an LLM caller correct its SQL and retry. Safe to
            # expose: the SQL is the caller's own text and the schema is public.
            raise InvalidQueryError(str(exc)) from None
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
