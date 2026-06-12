"""The SQL sandbox: read-only, no external access, single-statement, row cap, timeout."""

from __future__ import annotations

import time

import pytest

from idc_api.core.errors import InvalidQueryError, QueryTimeoutError
from idc_api.settings import Settings


@pytest.fixture(scope="module")
def backend():
    from idc_api.core.backend.duckdb_backend import DuckDBBackend

    return DuckDBBackend(Settings(sql_timeout_seconds=2.0, sql_max_rows=50))


def test_select_runs(backend):
    res = backend.run_user_sql("SELECT count(*) AS n FROM index")
    assert res.rows[0]["n"] > 1_000_000


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO index VALUES ()",
        "UPDATE index SET Modality='X'",
        "DELETE FROM index",
        "DROP TABLE index",
        "CREATE TABLE t AS SELECT 1",
        "ATTACH 'x.db'",
        "PRAGMA database_list",
        "SELECT 1; SELECT 2",  # multiple statements
    ],
)
def test_non_select_rejected(backend, sql):
    with pytest.raises(InvalidQueryError):
        backend.run_user_sql(sql)


def test_external_file_access_blocked(backend):
    # enable_external_access=false -> reading local files is denied by the engine, and the
    # engine's refusal surfaces as a clean caller-facing InvalidQueryError.
    with pytest.raises(InvalidQueryError) as exc:
        backend.run_user_sql("SELECT * FROM read_csv('/etc/passwd')")
    assert "disabled" in str(exc.value)


def test_engine_error_messages_reach_the_caller(backend):
    # DuckDB binder/catalog errors carry self-correction hints ("Candidate bindings",
    # "Did you mean") — they must surface as InvalidQueryError, not a generic internal
    # error, so an LLM caller can fix its SQL and retry.
    with pytest.raises(InvalidQueryError) as exc:
        backend.run_user_sql("SELECT no_such_column FROM index")
    assert "no_such_column" in str(exc.value)

    with pytest.raises(InvalidQueryError) as exc:
        backend.run_user_sql("SELECT * FROM analysis_results")
    assert "Did you mean" in str(exc.value)


def test_row_cap_truncates(backend):
    res = backend.run_user_sql("SELECT SeriesInstanceUID FROM index", max_rows=10)
    assert res.row_count == 10
    assert res.truncated is True


def test_statement_timeout(backend):
    t0 = time.time()
    with pytest.raises(QueryTimeoutError):
        backend.run_user_sql("SELECT count(*) FROM index a, index b", timeout_s=1.0)
    assert time.time() - t0 < 10


def test_comments_then_select_ok(backend):
    res = backend.run_user_sql("-- a comment\n/* block */ SELECT 1 AS one")
    assert res.rows == [{"one": 1}]
