"""Pydantic models — the shared contract returned by both the REST and MCP adapters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# --- discovery ----------------------------------------------------------------------------


class VersionInfo(BaseModel):
    idc_version: str = Field(..., description="IDC data release served, e.g. 'v24'.")
    idc_index_data_version: str = Field(..., description="Pinned idc-index-data package version.")


class Stats(BaseModel):
    idc_version: str
    collections: int
    analysis_results: int
    patients: int
    studies: int
    series: int
    instances: int
    size_TB: float


class CollectionSummary(BaseModel):
    collection_id: str
    collection_name: str | None = None
    cancer_types: str | None = None
    tumor_locations: str | None = None
    species: str | None = None
    subjects: int | None = None
    supporting_data: str | None = None
    description: str | None = None


class LicenseItem(BaseModel):
    license_short_name: str | None = None
    series: int
    size_TB: float


class CollectionDetail(CollectionSummary):
    patients: int
    studies: int
    series: int
    instances: int
    size_TB: float
    modalities: list[str] = []
    licenses: list[LicenseItem] = []


class AnalysisResult(BaseModel):
    analysis_result_id: str
    analysis_result_title: str | None = None
    source_DOI: str | None = None
    source_url: str | None = None
    subjects: int | None = None
    collections: str | None = None
    modalities: str | None = None
    license_short_name: str | None = None
    description: str | None = None


class AttributeInfo(BaseModel):
    name: str
    table: str
    data_type: str
    kind: str = Field(..., description="'term' (equality/IN) or 'range' (gte/lte).")
    categorical: bool = Field(
        ..., description="If true, get_attribute_values can enumerate its distinct values."
    )
    description: str


class AttributeValue(BaseModel):
    value: Any
    count: int


class AttributeValues(BaseModel):
    attribute: str
    values: list[AttributeValue]
    truncated: bool = False


# --- schema discovery ---------------------------------------------------------------------


class ColumnSchema(BaseModel):
    name: str
    type: str
    description: str = ""


class TableSchema(BaseModel):
    name: str
    description: str = ""
    columns: list[ColumnSchema]


class TableInfo(BaseModel):
    name: str
    description: str = ""
    column_count: int


class TableList(BaseModel):
    tables: list[TableInfo]


# --- cohort / manifest --------------------------------------------------------------------


class NumericRange(BaseModel):
    gte: float | str | None = None
    lte: float | str | None = None


class CohortFilters(BaseModel):
    """Structured filters over the main `index` table. ``terms`` does equality/IN (OR within
    an attribute, AND across attributes); ``ranges`` does gte/lte. Discover valid attribute
    names with ``list_attributes`` and valid values with ``get_attribute_values``."""

    terms: dict[str, list[str]] = Field(
        default_factory=dict,
        examples=[{"collection_id": ["nlst"], "Modality": ["CT"]}],
    )
    ranges: dict[str, NumericRange] = Field(default_factory=dict)


class CohortCounts(BaseModel):
    patients: int
    studies: int
    series: int
    instances: int
    size_TB: float


class SeriesManifestRow(BaseModel):
    collection_id: str | None = None
    PatientID: str | None = None
    StudyInstanceUID: str | None = None
    SeriesInstanceUID: str | None = None
    Modality: str | None = None
    SeriesDescription: str | None = None
    instanceCount: int | None = None
    series_size_MB: float | None = None
    aws_bucket: str | None = None
    crdc_series_uuid: str | None = None
    series_aws_url: str | None = None


class DownloadInfo(BaseModel):
    total_series: int
    size_TB: float
    idc_commands: list[str] = Field(
        default_factory=list,
        description="Ready-to-run commands using the `idc` CLI (from `pip install idc-index`).",
    )
    manifest_preview: list[str] = Field(
        default_factory=list, description="First few s3:// series URLs (sample)."
    )
    manifest_truncated: bool = Field(
        False, description="True if the selection exceeds the manifest enumeration cap."
    )
    note: str = ""


class ManifestResponse(BaseModel):
    counts: CohortCounts
    page: int
    page_size: int
    returned: int
    total_series: int
    series: list[SeriesManifestRow]
    download: DownloadInfo


# --- guarded SQL --------------------------------------------------------------------------


class SqlResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    max_rows: int


# --- viewer / citations / licenses --------------------------------------------------------


class ViewerURL(BaseModel):
    viewer_url: str
    viewer: str
    study_instance_uid: str | None = None
    series_instance_uid: str | None = None


class CitationsResult(BaseModel):
    format: str
    citations: list[Any]


class LicensesResult(BaseModel):
    licenses: list[LicenseItem]
