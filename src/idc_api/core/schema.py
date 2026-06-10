"""Table registry and filterable-attribute definitions.

Single source of truth for which idc-index tables v3 exposes, how they map to the bundled
Parquet, the rich per-column descriptions (from ``idc_index_data.INDEX_METADATA``), and the
curated set of attributes usable as cohort filters.

MVP exposes only the tables that ship *bundled* with ``idc-index-data`` (no network fetch).
Specialized indices (ct/mr/pt, seg/ann, sm, clinical) are added in later phases.
"""

from __future__ import annotations

from functools import lru_cache

import idc_index_data

# Exposed SQL table name -> key in idc_index_data.INDEX_METADATA.
# The main series-level table is exposed as ``index`` to match idc-index/idc docs
# (e.g. ``SELECT ... FROM index``), so SQL written for idc-index is portable here.
BUNDLED_TABLES: dict[str, str] = {
    "index": "idc_index",
    "collections_index": "collections_index",
    "analysis_results_index": "analysis_results_index",
    "version_metadata_index": "version_metadata_index",
    "prior_versions_index": "prior_versions_index",
}

MAIN_TABLE = "index"


def metadata_key(table: str) -> str:
    if table not in BUNDLED_TABLES:
        raise KeyError(table)
    return BUNDLED_TABLES[table]


def parquet_path(table: str) -> str:
    return str(idc_index_data.INDEX_METADATA[metadata_key(table)]["parquet_filepath"])


def list_table_names() -> list[str]:
    return list(BUNDLED_TABLES.keys())


@lru_cache(maxsize=None)
def table_schema(table: str) -> dict:
    """Return ``{name, description, columns:[{name,type,description}]}`` for a table,
    sourced from the idc-index schema JSON shipped in INDEX_METADATA."""
    meta = idc_index_data.INDEX_METADATA[metadata_key(table)]
    schema = meta.get("schema", {}) or {}
    columns = [
        {
            "name": c["name"],
            "type": c.get("type", ""),
            "description": c.get("description", "") or "",
        }
        for c in schema.get("columns", [])
    ]
    return {
        "name": table,
        "description": schema.get("table_description", "") or "",
        "columns": columns,
    }


# --- Filterable attributes (curated subset of the main `index` table) ---------------------
#
# ``kind`` controls how build_manifest interprets a filter and whether get_attribute_values
# will enumerate distinct values:
#   - "term": equality / IN over a (usually categorical) column. OR within an attribute,
#             AND across attributes (matching the v2 cohort semantics).
#   - "range": numeric or lexically-ordered (ISO date) column, filtered by gte/lte.
# ``categorical`` flags low-cardinality columns worth offering value-discovery for.

FILTERABLE_ATTRIBUTES: list[dict] = [
    {"name": "collection_id", "kind": "term", "categorical": True},
    {"name": "analysis_result_id", "kind": "term", "categorical": True},
    {"name": "PatientID", "kind": "term", "categorical": False},
    {"name": "StudyInstanceUID", "kind": "term", "categorical": False},
    {"name": "SeriesInstanceUID", "kind": "term", "categorical": False},
    {"name": "Modality", "kind": "term", "categorical": True},
    {"name": "BodyPartExamined", "kind": "term", "categorical": True},
    {"name": "Manufacturer", "kind": "term", "categorical": True},
    {"name": "ManufacturerModelName", "kind": "term", "categorical": True},
    {"name": "PatientSex", "kind": "term", "categorical": True},
    {"name": "sop_class_name", "kind": "term", "categorical": True},
    {"name": "license_short_name", "kind": "term", "categorical": True},
    {"name": "source_DOI", "kind": "term", "categorical": True},
    {"name": "instanceCount", "kind": "range", "categorical": False},
    {"name": "series_size_MB", "kind": "range", "categorical": False},
    {"name": "series_init_idc_version", "kind": "range", "categorical": False},
    {"name": "series_revised_idc_version", "kind": "range", "categorical": False},
    {"name": "StudyDate", "kind": "range", "categorical": False},
    {"name": "SeriesDate", "kind": "range", "categorical": False},
]

_ATTR_BY_NAME = {a["name"]: a for a in FILTERABLE_ATTRIBUTES}
TERM_ATTRIBUTES = {a["name"] for a in FILTERABLE_ATTRIBUTES if a["kind"] == "term"}
RANGE_ATTRIBUTES = {a["name"] for a in FILTERABLE_ATTRIBUTES if a["kind"] == "range"}


@lru_cache(maxsize=1)
def _index_column_descriptions() -> dict[str, dict]:
    return {c["name"]: c for c in table_schema(MAIN_TABLE)["columns"]}


@lru_cache(maxsize=1)
def index_columns() -> frozenset[str]:
    """Column names of the main `index` table (for validating identifiers we can't bind)."""
    return frozenset(_index_column_descriptions().keys())


def filterable_attributes() -> list[dict]:
    """Attributes for the `list_attributes` capability, enriched with type + description."""
    col_desc = _index_column_descriptions()
    out = []
    for a in FILTERABLE_ATTRIBUTES:
        col = col_desc.get(a["name"], {})
        out.append(
            {
                "name": a["name"],
                "table": MAIN_TABLE,
                "data_type": col.get("type", ""),
                "kind": a["kind"],
                "categorical": a["categorical"],
                "description": col.get("description", "") or "",
            }
        )
    return out


def is_filterable(attribute: str) -> bool:
    return attribute in _ATTR_BY_NAME


def attribute_kind(attribute: str) -> str:
    return _ATTR_BY_NAME[attribute]["kind"]
