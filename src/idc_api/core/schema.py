"""Table registry and filterable-attribute definitions.

Single source of truth for which idc-index tables v3 exposes, how they map to the bundled
Parquet, the rich per-column descriptions (from ``idc_index_data.INDEX_METADATA``), and the
curated set of attributes usable as cohort filters.

Two kinds of tables: BUNDLED ones ship as Parquet *inside the installed* ``idc-index-data``
package, so they need no downloads beyond installing it; SPECIALIZED ones (ct/mr/pt, seg/ann,
sm, clinical, …) are not in the package and are *fetched* from idc-index releases at build time
(see ``IDC_API_INCLUDE_INDICES`` and ``duckdb_backend.build_database_file``). Schemas for both
are bundled, so schema discovery works for a specialized table even before its data is fetched.

Including ``clinical_index`` additionally registers the per-collection clinical *data* tables
under the ``clinical`` schema (see ``CLINICAL_SCHEMA`` and ``duckdb_backend``); those are
discovered via the clinical service rather than this registry.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

import idc_index_data

# Exposed SQL table name -> key in idc_index_data.INDEX_METADATA.
# The main series-level table is exposed as ``index`` to match idc-index/idc docs
# (e.g. ``SELECT ... FROM index``), so SQL written for idc-index is portable here.
#
# BUNDLED tables ship as Parquet inside ``idc-index-data`` (no network). SPECIALIZED tables
# are published as release assets and *fetched* (via idc-index) at build time — their schemas
# ARE bundled, so schema discovery works without fetching; only the data needs downloading.
BUNDLED_TABLES: dict[str, str] = {
    "index": "idc_index",
    "collections_index": "collections_index",
    "analysis_results_index": "analysis_results_index",
    "version_metadata_index": "version_metadata_index",
    "prior_versions_index": "prior_versions_index",
}

# Specialized series-level indices. All join to ``index`` on SeriesInstanceUID except
# ``clinical_index`` (a per-collection data dictionary, joined on collection_id). The SQL
# table name equals the idc-index metadata key for each.
SPECIALIZED_TABLES: dict[str, str] = {
    "sm_index": "sm_index",
    "sm_instance_index": "sm_instance_index",
    "seg_index": "seg_index",
    "ann_index": "ann_index",
    "ann_group_index": "ann_group_index",
    "rtstruct_index": "rtstruct_index",
    "ct_index": "ct_index",
    "mr_index": "mr_index",
    "pt_index": "pt_index",
    "contrast_index": "contrast_index",
    "volume_geometry_index": "volume_geometry_index",
    "clinical_index": "clinical_index",
}

ALL_TABLES: dict[str, str] = {**BUNDLED_TABLES, **SPECIALIZED_TABLES}

MAIN_TABLE = "index"

# Per-collection clinical *data* tables (the actual clinical rows, e.g. ``nlst_canc``) are
# registered under this DuckDB schema and queried as ``clinical.<table>``. They are kept out of
# the main catalog (``list_tables``) and discovered via the clinical tools instead; they exist
# only when ``clinical_index`` is included in the build, and join to ``index`` on
# ``dicom_patient_id = PatientID``. See ``duckdb_backend._register_clinical_tables``.
CLINICAL_SCHEMA = "clinical"


def metadata_key(table: str) -> str:
    if table not in ALL_TABLES:
        raise KeyError(table)
    return ALL_TABLES[table]


def parquet_path(table: str) -> str:
    """Local Parquet path for a *bundled* table. Specialized tables have no bundled Parquet —
    obtain their path via the backend's fetch step instead."""
    return str(idc_index_data.INDEX_METADATA[metadata_key(table)]["parquet_filepath"])


def bundled_table_names() -> list[str]:
    return list(BUNDLED_TABLES.keys())


def specialized_table_names() -> list[str]:
    return list(SPECIALIZED_TABLES.keys())


def registered_table_names() -> list[str]:
    """Every table v3 knows how to describe (bundled + specialized), regardless of whether a
    given build actually included the specialized ones."""
    return list(ALL_TABLES.keys())


def resolve_include(setting: str | None) -> list[str]:
    """Parse the ``IDC_API_INCLUDE_INDICES`` setting into the specialized tables to build in.

    ``"all"`` -> every specialized table; ``"none"``/``""`` -> bundled only; otherwise a
    comma-separated allow-list of specialized table names. Returns names in registry order.
    """
    s = (setting or "").strip().lower()
    if s in ("", "none", "bundled", "base"):
        return []
    if s == "all":
        return list(SPECIALIZED_TABLES.keys())
    requested = {n.strip() for n in setting.split(",") if n.strip()}
    unknown = requested - set(SPECIALIZED_TABLES)
    if unknown:
        raise ValueError(
            f"Unknown specialized index/indices in IDC_API_INCLUDE_INDICES: "
            f"{', '.join(sorted(unknown))}. Valid: {', '.join(SPECIALIZED_TABLES)}."
        )
    return [n for n in SPECIALIZED_TABLES if n in requested]


def include_token(included: list[str]) -> str:
    """Short, stable token describing an included-specialized set, for cache-file naming."""
    if not included:
        return "base"
    if set(included) == set(SPECIALIZED_TABLES):
        return "all"
    digest = hashlib.sha1(",".join(sorted(included)).encode(), usedforsecurity=False).hexdigest()[
        :8
    ]
    return f"sub-{digest}"


def _column_type(c: dict) -> str:
    """Render a schema-JSON column type, folding the BigQuery-style ``mode`` field in:
    ``{type: STRING, mode: REPEATED}`` is an *array* column — ``STRING[]`` in DuckDB terms.
    Dropping the mode (as we used to) advertises arrays as plain strings, which steers SQL
    callers into predicates like ``col = 'x'`` / ``col LIKE ...`` that the engine rejects;
    match array elements with ``list_contains(col, 'x')`` instead."""
    t = c.get("type", "")
    return f"{t}[]" if c.get("mode") == "REPEATED" else t


# Local overrides for upstream idc-index-data *table* descriptions. Use ONLY for surface-routing
# guidance the upstream text gets wrong for an in-MCP client — same philosophy as the attribute
# ``note`` above. clinical_index's upstream description points retrieval at the external idc-index
# ``get_clinical_table()`` Python function, which is NOT reachable from this MCP; we repoint it at
# the in-MCP path (list_clinical_tables / run_sql against ``clinical.<table>``) and flag that those
# tables are irregular. Durable facts belong upstream; trim an override once upstream absorbs it.
TABLE_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "clinical_index": (
        "Dictionary (not data) for the per-collection clinical (non-imaging) tables that "
        "accompany imaging in IDC: one row per collection × clinical table × column. These "
        "tables do NOT follow the documented index schema and vary per collection. Discover them "
        "with list_clinical_tables and query each as clinical.<short_table_name> via run_sql "
        "(join to index on dicom_patient_id = index.PatientID). Use this table's column_label and "
        "value mappings to interpret their often-cryptic coded columns."
    ),
}


@lru_cache(maxsize=None)
def table_schema(table: str) -> dict:
    """Return ``{name, description, columns:[{name,type,description}]}`` for a table,
    sourced from the idc-index schema JSON shipped in INDEX_METADATA (table descriptions may be
    repointed inward via ``TABLE_DESCRIPTION_OVERRIDES``)."""
    meta = idc_index_data.INDEX_METADATA[metadata_key(table)]
    schema = meta.get("schema", {}) or {}
    columns = [
        {
            "name": c["name"],
            "type": _column_type(c),
            "description": c.get("description", "") or "",
        }
        for c in schema.get("columns", [])
    ]
    return {
        "name": table,
        "description": TABLE_DESCRIPTION_OVERRIDES.get(table) or schema.get("table_description", "") or "",
        "columns": columns,
    }


# --- Filterable attributes (curated subset of the main `index` table) ---------------------
#
# ``kind`` controls how build_manifest interprets a filter and whether get_attribute_values
# will enumerate distinct values:
#   - "term": equality / IN over a (usually categorical) column. OR within an attribute,
#             AND across attributes (the standard cohort-filter convention).
#   - "range": numeric or lexically-ordered (ISO date) column, filtered by gte/lte.
# ``categorical`` flags low-cardinality columns worth offering value-discovery for.
# ``note`` is a semantic caveat surfaced wherever the attribute is described (list_attributes
# descriptions and get_attribute_values responses) — use it when the obvious reading of an
# attribute is wrong for some series and the right column lives elsewhere. Durable facts
# belong upstream in the idc-index-data column descriptions (they flow in here on a pin bump);
# keep notes to surface-routing guidance and trim them once upstream absorbs the substance.

FILTERABLE_ATTRIBUTES: list[dict] = [
    {"name": "collection_id", "kind": "term", "categorical": True},
    {"name": "analysis_result_id", "kind": "term", "categorical": True},
    {"name": "PatientID", "kind": "term", "categorical": False},
    {"name": "StudyInstanceUID", "kind": "term", "categorical": False},
    {"name": "SeriesInstanceUID", "kind": "term", "categorical": False},
    {"name": "Modality", "kind": "term", "categorical": True},
    {
        "name": "BodyPartExamined",
        "kind": "term",
        "categorical": True,
        "note": (
            "Caution: this is the body region the source acquisition imaged — for derived "
            "series (SEG/RTSTRUCT) it does NOT say what was segmented. Segmented anatomy "
            "lives in seg_index.SegmentedPropertyType_CodeMeanings (an array column — join "
            "seg_index to index via SQL and match with list_contains)."
        ),
    },
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
        description = col.get("description", "") or ""
        note = a.get("note", "")
        if note:
            description = f"{description} {note}".strip()
        out.append(
            {
                "name": a["name"],
                "table": MAIN_TABLE,
                "data_type": col.get("type", ""),
                "kind": a["kind"],
                "categorical": a["categorical"],
                "description": description,
            }
        )
    return out


def attribute_note(attribute: str) -> str:
    """The semantic caveat for an attribute, or '' if none applies."""
    return _ATTR_BY_NAME.get(attribute, {}).get("note", "")


def is_filterable(attribute: str) -> bool:
    return attribute in _ATTR_BY_NAME


def attribute_kind(attribute: str) -> str:
    return _ATTR_BY_NAME[attribute]["kind"]
