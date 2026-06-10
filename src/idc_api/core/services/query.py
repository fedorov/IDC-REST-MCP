"""Schema discovery + the guarded read-only SQL tool."""

from __future__ import annotations

from .. import schema
from ..backend.base import QueryBackend
from ..errors import NotFoundError
from ..models import SqlResult, TableInfo, TableList, TableSchema


class QueryService:
    def __init__(self, backend: QueryBackend, settings):
        self.backend = backend
        self.settings = settings

    def list_tables(self) -> TableList:
        tables = []
        for name in self.backend.list_tables():
            sch = schema.table_schema(name)
            tables.append(
                TableInfo(
                    name=name,
                    description=sch["description"],
                    column_count=len(sch["columns"]),
                )
            )
        return TableList(tables=tables)

    def get_table_schema(self, table: str) -> TableSchema:
        if table not in self.backend.list_tables():
            raise NotFoundError(
                f"Unknown table: {table!r}. Available: {', '.join(self.backend.list_tables())}"
            )
        return TableSchema(**schema.table_schema(table))

    def run_sql(self, sql: str, max_rows: int | None = None) -> SqlResult:
        result = self.backend.run_user_sql(sql, max_rows=max_rows)
        return SqlResult(
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            max_rows=result.max_rows if result.max_rows is not None else self.settings.sql_max_rows,
        )
