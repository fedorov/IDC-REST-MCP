"""Clinical data discovery + access.

IDC ships two layers of clinical (non-imaging) metadata:

* ``clinical_index`` — a *data dictionary* (one row per collection × table × column, with a
  human-readable ``column_label`` and an array of coded ``values``). It is a normal specialized
  index, queryable via ``run_sql`` like any other table.
* The per-collection clinical *data* tables (e.g. ``nlst_canc``) — the actual rows. These are
  registered under the DuckDB ``clinical`` schema (see ``duckdb_backend._register_clinical_tables``)
  and queried as ``clinical.<table>``. They are kept out of ``list_tables`` (which would
  otherwise balloon by ~150 entries) and discovered through this service instead. Each joins to
  the main ``index`` on ``dicom_patient_id = index.PatientID``.

This service drives the discovery side from ``clinical_index`` and serves whole-table reads
from the registered ``clinical.<table>`` tables. Clinical data is present only when
``clinical_index`` was included in the build (``IDC_API_INCLUDE_INDICES``).
"""

from __future__ import annotations

from .. import schema
from ..backend.base import QueryBackend
from ..errors import NotFoundError
from ..models import (
    ClinicalTableInfo,
    ClinicalTableList,
    ColumnSchema,
    SqlResult,
    TableSchema,
)


class ClinicalService:
    def __init__(self, backend: QueryBackend, settings):
        self.backend = backend
        self.settings = settings

    # --- helpers --------------------------------------------------------------------------
    def _registered(self) -> set[str]:
        return set(self.backend.list_clinical_tables())

    def _require_clinical(self) -> set[str]:
        registered = self._registered()
        if not registered:
            raise NotFoundError(
                "Clinical data tables are not included in this build. Rebuild with "
                "IDC_API_INCLUDE_INDICES set to 'all' (the default) or a list including "
                "'clinical_index' to make them available."
            )
        return registered

    def _require_table(self, table: str) -> None:
        registered = self._require_clinical()
        if table not in registered:
            raise NotFoundError(
                f"Unknown clinical table: {table!r}. Use list_clinical_tables to discover "
                f"available tables."
            )

    # --- capabilities ---------------------------------------------------------------------
    def list_clinical_tables(self, collection_id: str | None = None) -> ClinicalTableList:
        """List the per-collection clinical data tables, optionally filtered to one collection.

        Sourced from ``clinical_index`` (collection + column count per table), intersected with
        the tables actually registered in the ``clinical`` schema so the listing reflects what
        is queryable.
        """
        registered = self._require_clinical()
        sql = (
            'SELECT collection_id, short_table_name, COUNT(*) AS column_count '
            "FROM clinical_index "
        )
        params: list = []
        if collection_id:
            sql += "WHERE collection_id = ? "
            params.append(collection_id)
        sql += "GROUP BY collection_id, short_table_name ORDER BY collection_id, short_table_name"

        result = self.backend.query(sql, params or None)
        tables = [
            ClinicalTableInfo(
                table_name=row["short_table_name"],
                sql_path=f"{schema.CLINICAL_SCHEMA}.{row['short_table_name']}",
                collection_id=row["collection_id"],
                column_count=int(row["column_count"]),
            )
            for row in result.rows
            if row["short_table_name"] in registered
        ]
        return ClinicalTableList(tables=tables)

    def get_clinical_table_schema(self, table: str) -> TableSchema:
        """Columns of a clinical table — names and DuckDB types introspected from the live
        table, enriched with human-readable ``column_label`` descriptions from
        ``clinical_index`` (column names are often cryptic; the label says what they mean)."""
        self._require_table(table)
        cols = self.backend.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema.CLINICAL_SCHEMA, table],
        )
        labels = self.backend.query(
            'SELECT "column" AS column_name, column_label FROM clinical_index '
            "WHERE short_table_name = ?",
            [table],
        )
        label_by_col = {r["column_name"]: (r.get("column_label") or "") for r in labels.rows}
        columns = [
            ColumnSchema(
                name=r["column_name"],
                type=r["data_type"],
                description=label_by_col.get(r["column_name"], ""),
            )
            for r in cols.rows
        ]
        return TableSchema(
            name=f"{schema.CLINICAL_SCHEMA}.{table}",
            description=(
                "Per-collection clinical data. Join to `index` on "
                "dicom_patient_id = index.PatientID."
            ),
            columns=columns,
        )

    def get_clinical_table(self, table: str, max_rows: int | None = None) -> SqlResult:
        """Return the rows of a clinical table (capped). For relational questions — joining to
        imaging, filtering by clinical attributes — use ``run_sql`` against ``clinical.<table>``
        instead."""
        self._require_table(table)
        max_rows = max_rows if max_rows is not None else self.settings.sql_max_rows
        # Identifier validated against the registered clinical set above (never a raw value);
        # double-quoted because identifiers can't be bound as parameters (invariant #4).
        result = self.backend.query(
            f'SELECT * FROM "{schema.CLINICAL_SCHEMA}"."{table}"',
            None,
            max_rows=max_rows,
        )
        return SqlResult(
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            max_rows=max_rows,
        )
