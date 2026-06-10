"""Discovery: versions, stats, collections, analysis results, attributes, attribute values."""

from __future__ import annotations

import idc_index_data

from .. import schema
from ..backend.base import QueryBackend
from ..errors import InvalidQueryError, NotFoundError
from ..models import (
    AnalysisResult,
    AttributeInfo,
    AttributeValue,
    AttributeValues,
    CollectionDetail,
    CollectionSummary,
    LicenseItem,
    Stats,
    VersionInfo,
)

_MB_PER_TB = 1_000_000


def _idc_version() -> str:
    major = idc_index_data.__version__.split(".")[0]
    return f"v{major}"


class DiscoveryService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def version(self) -> VersionInfo:
        return VersionInfo(
            idc_version=_idc_version(),
            idc_index_data_version=idc_index_data.__version__,
        )

    def stats(self) -> Stats:
        agg = self.backend.query(
            "SELECT count(DISTINCT PatientID) patients, "
            "count(DISTINCT StudyInstanceUID) studies, "
            "count(DISTINCT SeriesInstanceUID) series, "
            "COALESCE(sum(instanceCount),0) instances, "
            "COALESCE(sum(series_size_MB),0) size_mb FROM index"
        ).rows[0]
        collections = self.backend.query("SELECT count(*) c FROM collections_index").rows[0]["c"]
        analysis = self.backend.query(
            "SELECT count(*) c FROM analysis_results_index"
        ).rows[0]["c"]
        return Stats(
            idc_version=_idc_version(),
            collections=collections,
            analysis_results=analysis,
            patients=agg["patients"],
            studies=agg["studies"],
            series=agg["series"],
            instances=int(agg["instances"]),
            size_TB=round(agg["size_mb"] / _MB_PER_TB, 3),
        )

    def list_collections(self) -> list[CollectionSummary]:
        rows = self.backend.query(
            "SELECT collection_id, collection_name, cancer_types, tumor_locations, "
            "species, subjects, supporting_data, description "
            "FROM collections_index ORDER BY collection_id"
        ).rows
        return [CollectionSummary(**r) for r in rows]

    def get_collection(self, collection_id: str) -> CollectionDetail:
        meta_rows = self.backend.query(
            "SELECT collection_id, collection_name, cancer_types, tumor_locations, "
            "species, subjects, supporting_data, description "
            "FROM collections_index WHERE collection_id = ?",
            [collection_id],
        ).rows
        if not meta_rows:
            raise NotFoundError(f"Collection not found: {collection_id!r}")
        meta = meta_rows[0]

        agg = self.backend.query(
            "SELECT count(DISTINCT PatientID) patients, "
            "count(DISTINCT StudyInstanceUID) studies, "
            "count(DISTINCT SeriesInstanceUID) series, "
            "COALESCE(sum(instanceCount),0) instances, "
            "COALESCE(sum(series_size_MB),0) size_mb "
            "FROM index WHERE collection_id = ?",
            [collection_id],
        ).rows[0]
        modalities = [
            r["Modality"]
            for r in self.backend.query(
                "SELECT DISTINCT Modality FROM index WHERE collection_id = ? "
                "AND Modality IS NOT NULL ORDER BY Modality",
                [collection_id],
            ).rows
        ]
        licenses = self._licenses_for(["collection_id = ?"], [collection_id])

        return CollectionDetail(
            **meta,
            patients=agg["patients"],
            studies=agg["studies"],
            series=agg["series"],
            instances=int(agg["instances"]),
            size_TB=round(agg["size_mb"] / _MB_PER_TB, 3),
            modalities=modalities,
            licenses=licenses,
        )

    def _licenses_for(self, where: list[str], params: list) -> list[LicenseItem]:
        clause = " AND ".join(where) if where else "TRUE"
        rows = self.backend.query(
            f"SELECT license_short_name, count(DISTINCT SeriesInstanceUID) series, "
            f"COALESCE(sum(series_size_MB),0) size_mb FROM index WHERE {clause} "
            f"GROUP BY 1 ORDER BY series DESC",
            params,
        ).rows
        return [
            LicenseItem(
                license_short_name=r["license_short_name"],
                series=r["series"],
                size_TB=round(r["size_mb"] / _MB_PER_TB, 3),
            )
            for r in rows
        ]

    def list_analysis_results(self) -> list[AnalysisResult]:
        rows = self.backend.query(
            "SELECT analysis_result_id, analysis_result_title, source_DOI, source_url, "
            "subjects, collections, modalities, license_short_name, description "
            "FROM analysis_results_index ORDER BY analysis_result_id"
        ).rows
        return [AnalysisResult(**r) for r in rows]

    def list_attributes(self) -> list[AttributeInfo]:
        return [AttributeInfo(**a) for a in schema.filterable_attributes()]

    def get_attribute_values(self, attribute: str, limit: int = 100) -> AttributeValues:
        if attribute not in schema.index_columns():
            raise InvalidQueryError(
                f"Unknown attribute: {attribute!r}. Use list_attributes / get_table_schema."
            )
        limit = max(1, min(int(limit), 10000))
        res = self.backend.query(
            f'SELECT "{attribute}" AS value, count(*) AS count FROM index '
            f'WHERE "{attribute}" IS NOT NULL GROUP BY 1 ORDER BY count DESC LIMIT {limit + 1}',
        )
        truncated = len(res.rows) > limit
        rows = res.rows[:limit]
        return AttributeValues(
            attribute=attribute,
            values=[AttributeValue(value=r["value"], count=r["count"]) for r in rows],
            truncated=truncated,
        )
