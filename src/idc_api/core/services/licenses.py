"""License breakdown for a cohort (CC-BY vs CC-BY-NC vs custom)."""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..filters import compile_filters
from ..models import CohortFilters, LicenseItem, LicensesResult

_MB_PER_TB = 1_000_000


class LicenseService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def get_licenses(self, filters: CohortFilters) -> LicensesResult:
        where, params = compile_filters(filters)
        rows = self.backend.query(
            f"SELECT license_short_name, count(DISTINCT SeriesInstanceUID) series, "
            f"COALESCE(sum(series_size_MB),0) size_mb FROM index WHERE {where} "
            f"GROUP BY 1 ORDER BY series DESC",
            params,
        ).rows
        return LicensesResult(
            licenses=[
                LicenseItem(
                    license_short_name=r["license_short_name"],
                    series=r["series"],
                    size_TB=round(r["size_mb"] / _MB_PER_TB, 3),
                )
                for r in rows
            ]
        )
