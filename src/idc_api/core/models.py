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
    note: str = Field("", description="Semantic caveat about this attribute, when one applies.")


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


class ClinicalTableInfo(BaseModel):
    """A per-collection clinical data table (e.g. ``nlst_canc``), queryable in SQL as
    ``clinical.<table_name>`` and joinable to ``index`` on ``dicom_patient_id = PatientID``."""

    table_name: str = Field(..., description="Short table name; query as clinical.<table_name>.")
    sql_path: str = Field(
        ...,
        description="Ready-to-use FROM target for run_sql, e.g. clinical.nlst_canc — use this "
        "verbatim rather than reconstructing it from table_name.",
    )
    collection_id: str = Field(..., description="Collection this clinical table belongs to.")
    column_count: int = Field(..., description="Number of documented columns (from clinical_index).")
    description: str = ""


class ClinicalTableList(BaseModel):
    tables: list[ClinicalTableInfo]


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
    citations: list[Any]  # per-dataset citations, from the cohort's source DOIs
    idc_acknowledgment: Any | None = None  # citation for the IDC paper (10.1148/rg.230180)
    recommendation: str = (
        "In addition to the per-dataset citations, always acknowledge IDC itself by citing "
        "Fedorov et al., https://doi.org/10.1148/rg.230180 (see idc_acknowledgment)."
    )


class LicensesResult(BaseModel):
    licenses: list[LicenseItem]
