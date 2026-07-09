"""IDC MCP server.

Hand-authored Model Context Protocol tools over the same core services the REST API uses.
Tool descriptions are prescriptive about *when* to call each tool (this measurably improves
tool selection on current LLMs). Discovery tools (``list_attributes``, ``get_attribute_values``,
``list_tables``, ``get_table_schema``) exist so an agent grounds filters and SQL in real
columns/values instead of guessing.

Transports:
  * stdio (default) — runs locally on the user's machine; ``download_cohort`` can fetch files.
  * streamable-http (``--http``) — hosted/shared; download is disabled (manifests only).
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import json
import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from ..core import schema as core_schema
from ..core import version as core_version
from ..core.context import AppContext
from ..core.errors import IDCAPIError
from ..core.models import CohortFilters, NumericRange
from ..settings import get_settings

logger = logging.getLogger("idc_api.mcp")

INSTRUCTIONS = """\
This server exposes the NCI Imaging Data Commons (IDC) — public, open cancer imaging (DICOM)
data; no authentication needed. The main table is `index` (one row per series); collection_id
(source dataset) and analysis_result_id (derived datasets) group it.

Work this way:
1. Ground first — list_attributes + get_attribute_values for valid filter values; list_tables +
   get_table_schema before SQL. Do not guess values or column names.
2. build_cohort for attribute filters; run_sql for relational/aggregate questions (read-only
   DuckDB; `index` plus specialized indices joined on SeriesInstanceUID and named for the
   DICOM Modality they detail — seg_index: what anatomy a SEG segments; ct_index, mr_index,
   pt_index: acquisition; sm_index, ann_index: microscopy; …). If a property isn't in
   list_attributes (e.g.
   segmented anatomy), check list_tables before concluding it's unavailable. For clinical
   (non-imaging) attributes — staging, demographics, therapy — use list_clinical_tables, then
   get_clinical_table_schema / get_clinical_table to read the rows (or run_sql against
   `clinical.<table>`).
3. IDC is large (100+ TB) — always report counts/size_TB and warn before any download.
   download_cohort transfers files only when the server runs locally; otherwise use
   get_cohort_urls / the returned `idc` commands.
Cite with get_citations (per-dataset citations plus the IDC paper to acknowledge IDC itself);
respect get_licenses (CC-BY vs CC-BY-NC). See `idc://guide` for the data model, the full tool
list, and join examples."""


# stateless_http=True / json_response=True make the hosted (streamable-http) transport
# horizontally scalable on Cloud Run.
#
# Why: MCP's streamable-HTTP transport is normally *session-oriented* — the server keeps each
# client's session state in the memory of the process that handled `initialize`, and every
# follow-up request (and the server->client SSE stream) must return to that same process. Cloud
# Run load-balances requests across autoscaled instances with no sticky routing, so a follow-up
# request can land on a different instance that has never seen the session and the connection
# breaks. Running a single pinned instance or relying on best-effort session affinity only
# papers over this.
#
# Going stateless removes the per-process session entirely: each request is self-contained, so
# the service scales out behind a plain load balancer exactly like the REST API. We lose nothing
# by doing this because this server only exposes client-initiated tools + static resources — it
# uses none of the features that require a persistent session (server->client sampling,
# elicitation, resource-change subscriptions, streamed partial progress). json_response=True
# returns plain JSON instead of an SSE stream, which is the natural fit for stateless request/
# response. These flags affect ONLY the HTTP transport; local stdio mode is unaffected.
def _transport_security() -> TransportSecuritySettings:
    """Configure the HTTP transport's DNS-rebinding (Host/Origin allow-list) protection.

    The SDK defaults to allow-listing localhost only, so behind a hosted domain (Cloud Run) it
    rejects requests with HTTP 421 ("Invalid Host header"). This service is public,
    unauthenticated, and read-only, so we default the protection off (see settings). Operators
    who want it on set IDC_API_MCP_DNS_REBINDING_PROTECTION=true plus IDC_API_MCP_ALLOWED_HOSTS
    / _ALLOWED_ORIGINS for their domain.
    """
    s = get_settings()
    if s.mcp_dns_rebinding_protection:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=s.mcp_allowed_hosts,
            allowed_origins=s.mcp_allowed_origins,
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _server_version() -> str:
    """Version advertised in the MCP initialize handshake (serverInfo.version).

    Base is the installed package version; when a deploy sets IDC_API_BUILD (e.g. a short git
    SHA) it is appended as a PEP 440 local segment so the string moves on every redeploy — this
    is how a caller confirms which build a hosted instance is actually running. Without setting
    `version=`, FastMCP would fall back to the MCP SDK's own version, which says nothing about
    this server. Shared with the REST adapter (which reports the same string via /v3/version and
    /v3/openapi.json) through core.version.
    """
    return core_version.server_version()


mcp = FastMCP(
    "IDC (Imaging Data Commons)",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security(),
)
# FastMCP doesn't forward a version to the low-level server; left unset, the initialize
# handshake reports the MCP SDK's own version (meaningless for tracking this server). Set it on
# the underlying server so serverInfo.version reflects our build. No public accessor exists in
# this SDK version, hence the _mcp_server reach-in.
mcp._mcp_server.version = _server_version()
ctx = AppContext()


def _format_sql(sql: str) -> str:
    """Render `sql` for the audit log per IDC_API_SQL_LOG_MODE: a capped readable snippet
    (default), or a short digest that lets callers correlate repeated identical queries without
    putting query text in logs at all."""
    s = get_settings()
    if s.sql_log_mode == "hash":
        return "sha256:" + hashlib.sha256(sql.encode()).hexdigest()[:12]
    return sql[: s.sql_log_chars]


def _log_call(
    tool: str,
    start: float,
    outcome: str,
    *,
    result: Any = None,
    error: str | None = None,
    sql: str | None = None,
) -> None:
    """One structured line per tool call. All tools share one HTTP endpoint on the hosted
    transport, so the platform's own request log can't tell them apart — this is what makes
    per-tool call volume/latency/errors visible for abuse detection. Result rows are never
    logged, only shape (row_count/truncated) already meant to be caller-visible; `sql` (when the
    tool takes one) is rendered per _format_sql."""
    entry: dict[str, Any] = {
        "tool": tool,
        "outcome": outcome,
        "duration_ms": round((time.monotonic() - start) * 1000, 1),
    }
    if sql is not None:
        entry["sql"] = _format_sql(sql)
    if error is not None:
        entry["error"] = error
    if isinstance(result, dict):
        rows = result.get("rows")
        if isinstance(rows, list):
            entry["row_count"] = len(rows)
        if "truncated" in result:
            entry["truncated"] = result["truncated"]
    logger.info(json.dumps(entry))


def guard(fn):
    """Convert core errors into clean MCP tool errors; never leak tracebacks. Also emits an
    audit-log line per call (see _log_call)."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            sql = sig.bind_partial(*args, **kwargs).arguments.get("sql")
        except TypeError:
            sql = None
        try:
            result = fn(*args, **kwargs)
        except IDCAPIError as exc:
            _log_call(fn.__name__, start, "error", error=exc.code, sql=sql)
            raise ToolError(exc.message) from None
        except ToolError:
            _log_call(fn.__name__, start, "error", error="tool_error", sql=sql)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in MCP tool %s", fn.__name__)
            _log_call(fn.__name__, start, "error", error="internal", sql=sql)
            raise ToolError("Internal error while handling the request.") from None
        _log_call(fn.__name__, start, "ok", result=result, sql=sql)
        return result

    return wrapper


def _filters(terms: dict | None, ranges: dict | None) -> CohortFilters:
    return CohortFilters(
        terms=terms or {},
        ranges={k: NumericRange(**v) for k, v in (ranges or {}).items()},
    )


# --- discovery ----------------------------------------------------------------------------


@mcp.tool()
@guard
def get_idc_version() -> dict:
    """Return the IDC data release served (e.g. 'v24') and pinned idc-index version, plus this
    server's own software version (`api_version`, and `build` if the deploy stamped one). Call
    this to confirm which IDC version your answers are based on — and which build of the server
    produced them."""
    return ctx.discovery.version().model_dump(mode="json")


@mcp.tool()
@guard
def get_stats() -> dict:
    """Headline totals for the whole of IDC: number of collections, analysis results,
    patients, studies, series, instances, and total size in TB."""
    return ctx.discovery.stats().model_dump(mode="json")


@mcp.tool()
@guard
def list_collections() -> list[dict]:
    """List all IDC collections (original imaging datasets) with cancer types, tumor
    locations, species, and subject counts. Use to find a collection_id for filtering."""
    return [c.model_dump(mode="json") for c in ctx.discovery.list_collections()]


@mcp.tool()
@guard
def get_collection(collection_id: str) -> dict:
    """Detailed metadata for one collection: description, subject/series/instance counts,
    total size, the modalities present, and the license breakdown."""
    return ctx.discovery.get_collection(collection_id).model_dump(mode="json")


@mcp.tool()
@guard
def list_analysis_results() -> list[dict]:
    """List IDC analysis results — derived datasets (AI/expert segmentations, annotations,
    radiomics) layered on the original collections. Use to find an analysis_result_id."""
    return [a.model_dump(mode="json") for a in ctx.discovery.list_analysis_results()]


@mcp.tool()
@guard
def list_attributes() -> list[dict]:
    """List the attributes you can filter a cohort by (name, type, whether categorical).
    Call this before build_cohort to learn valid filter attribute names. These are a curated
    subset of the `index` table chosen for cohort filtering — run_sql can query or filter on any
    column in any table from list_tables, including `index` columns that are not filter
    attributes."""
    return [a.model_dump(mode="json") for a in ctx.discovery.list_attributes()]


@mcp.tool()
@guard
def get_attribute_values(attribute: str, limit: int = 50) -> dict:
    """Return the distinct values (with counts) of a categorical attribute on the `index`
    table, e.g. attribute='Modality' or 'BodyPartExamined'. ALWAYS call this before
    filtering by an attribute so you use real values and correct casing — do not guess.
    Keep `limit` small while exploring; the result carries a `truncated` flag — if true you did
    NOT get every distinct value, so raise `limit` and re-check. `limit` is capped server-side
    (so a large value returns all values for any realistic categorical attribute); a
    `truncated=false` response means you have the complete list."""
    return ctx.discovery.get_attribute_values(attribute, limit=limit).model_dump(mode="json")


# --- schema discovery ---------------------------------------------------------------------


@mcp.tool()
@guard
def list_tables() -> dict:
    """List the tables available to run_sql: the main `index`, collection/analysis/version
    metadata tables, and the specialized indices — named `<modality>_index` after the DICOM
    Modality they describe (seg_index: segmented anatomy of SEG series; ct_index, mr_index,
    pt_index: acquisition parameters; sm_index, ann_index: microscopy), plus contrast_index,
    volume_geometry_index, and clinical_index.
    Call this before writing SQL, and whenever a property you need (e.g. what a segmentation
    contains) is not a filterable attribute — it may live in a specialized index. Per-collection
    clinical data tables are listed separately by list_clinical_tables (queried as
    `clinical.<table>`), not here."""
    return ctx.query.list_tables().model_dump(mode="json")


@mcp.tool()
@guard
def get_table_schema(table: str) -> dict:
    """Return the columns (name, type, description) of a table. Call this before run_sql to
    use correct column names. Use table='index' for the main series-level table."""
    return ctx.query.get_table_schema(table).model_dump(mode="json")


# --- clinical data ------------------------------------------------------------------------


@mcp.tool()
@guard
def list_clinical_tables(collection_id: str | None = None) -> dict:
    """Discover the per-collection clinical (non-imaging) data tables — demographics, diagnoses,
    cancer staging, therapies, labs, outcomes. Call this before querying any clinical attribute:
    clinical data is NOT a filterable attribute and is not harmonized across collections, so the
    table and column names vary per collection. Pass `collection_id` to narrow to one collection.
    Each table is queryable in run_sql as `clinical.<table_name>` and joins to `index` on
    dicom_patient_id = index.PatientID. (For *what columns mean*, also see the `clinical_index`
    dictionary table.)"""
    return ctx.clinical.list_clinical_tables(collection_id=collection_id).model_dump(mode="json")


@mcp.tool()
@guard
def get_clinical_table_schema(table: str) -> dict:
    """Return the columns of a clinical table (name, DuckDB type, and a human-readable label
    from clinical_index — clinical column names are often cryptic). Call this before run_sql or
    get_clinical_table. Get the table name from list_clinical_tables."""
    return ctx.clinical.get_clinical_table_schema(table).model_dump(mode="json")


@mcp.tool()
@guard
def get_clinical_table(table: str, max_rows: int = 100) -> dict:
    """Return the rows of a clinical table (capped at max_rows). Use to inspect a small clinical
    table directly; for filtering by clinical attributes or joining to imaging, use run_sql
    against `clinical.<table>` instead. Get the table name from list_clinical_tables."""
    return ctx.clinical.get_clinical_table(table, max_rows=max_rows).model_dump(mode="json")


# --- cohort / query -----------------------------------------------------------------------


@mcp.tool()
@guard
def build_cohort(
    terms: dict | None = None,
    ranges: dict | None = None,
    page: int = 0,
    page_size: int = 5,
) -> dict:
    """Build a cohort from structured filters and get back distinct counts (patients/studies/
    series/instances/size_TB), a sample of matching series, and a download payload (idc
    commands + manifest preview).

    `terms` is {attribute: [values]} for equality/IN (e.g. {"Modality": ["MR"],
    "BodyPartExamined": ["BREAST"]}). `ranges` is {attribute: {"gte": x, "lte": y}} for
    numeric/date ranges. Discover valid attributes with list_attributes and valid values with
    get_attribute_values. For anything these structured filters can't express, use run_sql."""
    f = _filters(terms, ranges)
    return ctx.cohort.build_manifest(f, page=page, page_size=page_size).model_dump(mode="json")


@mcp.tool()
@guard
def run_sql(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL SELECT against the IDC index using DuckDB and return the rows.
    Use for anything build_cohort can't express (GROUP BY, joins across tables, custom
    aggregations, or filtering on columns that are not filter attributes: the attributes from
    list_attributes are a curated subset of `index`, so run_sql is how you reach the rest —
    other `index` columns like SeriesDescription or PatientAge, and columns that exist only in a
    specialized index such as segmented anatomy in seg_index). Only a single read-only SELECT/WITH
    statement is allowed; the
    connection is sandboxed (no writes, no file/network access). Call list_tables /
    get_table_schema first to use correct table and column names. The main table is `index`.
    Per-collection clinical tables are in the `clinical` schema (query as `clinical.<table>`,
    discover via list_clinical_tables) and join to index on dicom_patient_id = index.PatientID.
    Keep `max_rows` small while exploring (e.g. to peek at a few rows or confirm a query is
    right) and raise it only once you actually need the full result — large results bloat the
    response. Prefer aggregating in SQL (COUNT/GROUP BY) over fetching many raw rows. The result
    carries a `truncated` flag: if true you did NOT get every row — narrow/aggregate the query,
    or raise `max_rows` and re-check the flag (it is the only reliable 'is this complete?'
    signal). `max_rows` is clamped to a server ceiling, so there is no 'unbounded' value; for
    bulk series use build_cohort / get_cohort_urls instead of dumping rows here."""
    return ctx.query.run_sql(sql, max_rows=max_rows).model_dump(mode="json")


# --- retrieval / tools --------------------------------------------------------------------


@mcp.tool()
@guard
def get_cohort_urls(
    terms: dict | None = None,
    ranges: dict | None = None,
    source: str = "aws",
    limit: int = 100,
) -> dict:
    """Get public download URLs (one s3:// or gs:// per series) for a filtered cohort.
    `source` is 'aws' or 'gcs'. Returns up to `limit` URLs (increase for full manifests).
    These are anonymous public URLs — download with s5cmd/gsutil or the `idc` CLI."""
    f = _filters(terms, ranges)
    urls, truncated = ctx.manifest.manifest_lines(f, source=source, limit=limit)
    return {
        "source": source,
        "returned": len(urls),
        "truncated": truncated,
        "urls": urls,
        "note": "Anonymous public URLs; append '/*' is a series prefix wildcard for s5cmd.",
    }


@mcp.tool()
@guard
def get_viewer_url(
    series_instance_uid: str | None = None,
    study_instance_uid: str | None = None,
    viewer: str | None = None,
) -> dict:
    """Get a browser viewer URL (OHIF for radiology, SLIM for slide microscopy) for a series
    or study, so the user can view the images without downloading. Provide a
    SeriesInstanceUID or StudyInstanceUID (get one from build_cohort or run_sql)."""
    return ctx.viewer.get_viewer_url(
        series_instance_uid=series_instance_uid,
        study_instance_uid=study_instance_uid,
        viewer=viewer,
    ).model_dump(mode="json")


@mcp.tool()
@guard
def get_citations(
    terms: dict | None = None,
    ranges: dict | None = None,
    citation_format: str = "apa",
) -> dict:
    """Get the publications to cite for a cohort: per-dataset citations (from the cohort's
    source DOIs) in `citations`, plus the IDC paper in `idc_acknowledgment`. `citation_format`
    is one of: apa, bibtex, csl-json, turtle. When the user publishes results using IDC data,
    always include the per-dataset citations AND acknowledge IDC itself via `idc_acknowledgment`
    (see the `recommendation` field)."""
    f = _filters(terms, ranges)
    return ctx.citations.get_citations(f, citation_format=citation_format).model_dump(mode="json")


@mcp.tool()
@guard
def get_licenses(terms: dict | None = None, ranges: dict | None = None) -> dict:
    """Get the license breakdown (series count + size per license) for a cohort. Use to check
    whether data is commercial-friendly (CC BY) or non-commercial only (CC BY-NC) before the
    user reuses it."""
    f = _filters(terms, ranges)
    return ctx.licenses.get_licenses(f).model_dump(mode="json")


@mcp.tool()
@guard
def download_cohort(
    download_dir: str,
    collection_id: list[str] | None = None,
    patientId: list[str] | None = None,
    studyInstanceUID: list[str] | None = None,
    seriesInstanceUID: list[str] | None = None,
    dry_run: bool = True,
    source: str = "aws",
) -> dict:
    """Download DICOM files for a selection to a local directory (via idc-index/s5cmd). Only
    works when this MCP server runs locally on the user's machine; otherwise it returns a
    clear error and you should use get_cohort_urls / the idc commands instead. Start with
    dry_run=True to report the size, confirm with the user, then dry_run=False."""
    return ctx.download.download(
        download_dir=download_dir,
        collection_id=collection_id,
        patientId=patientId,
        studyInstanceUID=studyInstanceUID,
        seriesInstanceUID=seriesInstanceUID,
        dry_run=dry_run,
        source_bucket_location=source,
    )


# --- resources ----------------------------------------------------------------------------

_GUIDE = """\
# Querying IDC via this MCP server

**Data model.** IDC stores public cancer imaging as DICOM, organized as
Patient → Study → Series, with two *independent* grouping labels: `collection_id` (the source
dataset, e.g. `nlst`, `tcga_luad`; a patient is in exactly one) and `analysis_result_id` (a
derived dataset — segmentations/annotations — that can span *multiple* collections, so it is
not nested under one). The main table is `index` (one row per *series*). IDC is large
(~100+ TB) — always check size before suggesting a download.

**The tools form a few families that build on each other:**
- *Discovery* (`get_stats`, `list_collections`, `get_collection`, `list_analysis_results`,
  `list_attributes`, `get_attribute_values`) — what exists, and the *vocabulary* (attribute
  names + valid values) you filter on.
- *Cohort* (`build_cohort`) — turn a chosen combination of that vocabulary into distinct
  counts + a sample of series + a download payload.
- *Retrieval* (`get_cohort_urls`, `download_cohort`) — the download half: public URLs / files.
- *SQL* (`list_tables`, `get_table_schema`, `run_sql`) — the escape hatch for anything
  `build_cohort` can't express (GROUP BY, joins, custom aggregations).
- *Clinical* (`list_clinical_tables`, `get_clinical_table_schema`, `get_clinical_table`) —
  discover and read per-collection clinical (non-imaging) data; also queryable via `run_sql`.
- *Side tools* (`get_viewer_url`, `get_citations`, `get_licenses`) — view / cite / license-check
  a cohort.

Prefer `build_cohort` for common cases (structured, can't be malformed); reach for `run_sql`
only when it can't express your query. Discovery feeds Cohort; Cohort reuses Retrieval to build
its payload — so a typical request flows Discovery → Cohort → Retrieval, with SQL as a bypass.

**Recommended workflow:**
1. *Find data:* `list_collections` / `get_collection` (imaging datasets), `list_analysis_results`
   (derived annotations & segmentations).
2. *Ground filters (do this first to avoid wrong values):* `list_attributes` → valid attributes;
   `get_attribute_values(attribute=...)` → valid values + counts (correct casing!). If the
   property you need is not there (e.g. what anatomy a segmentation contains), it likely lives
   in a specialized index — see *Tables for run_sql* below.
3. *Build:* `build_cohort(terms={...}, ranges={...})` → counts, sample series, download payload.
   For complex queries: `list_tables` → `get_table_schema('index')` → `run_sql('SELECT ...')`.
   *Explore narrow, then widen:* keep result sizes small while you're still figuring out the
   query (small `max_rows` / `limit` / `page_size`, or COUNT/GROUP BY instead of raw rows), and
   only raise the limit once you know you need the full set — large responses waste context.
   *How to tell you got everything:* size-capped responses carry a `truncated` flag —
   `truncated=false` means the result is complete; `true` means raise the limit and re-check (or
   narrow/aggregate). `run_sql`'s `max_rows` is clamped to a server ceiling, so there is no
   'unlimited' value — for bulk *series* use the cohort/manifest tools, not raw `run_sql` rows.
4. *Get the data:* `get_cohort_urls` returns public s3:///gs:// URLs; the `build_cohort`
   response also includes ready-to-run `idc` CLI commands. `download_cohort` performs a real
   local download only when the server runs on your machine.
5. *Be a good citizen:* check `get_licenses` (CC BY vs CC BY-NC) and, when publishing, include
   `get_citations` output — both the per-dataset `citations` and `idc_acknowledgment` (the IDC
   paper, https://doi.org/10.1148/rg.230180) to acknowledge IDC itself.

**Tables for run_sql.** Bundled: `index` (series), `collections_index`,
`analysis_results_index`, `version_metadata_index`, `prior_versions_index`. Specialized indices
hold the modality-specific metadata `index` lacks and are named `<modality>_index` after the
DICOM Modality of the series they describe — when a Modality value is central to your question
(SEG, CT, SM, …), check its index. They join to `index` on `SeriesInstanceUID`: `seg_index` /
`ann_index` / `ann_group_index` / `rtstruct_index` (segmentations/annotations — *what anatomy
was segmented* via `SegmentedPropertyType_CodeMeanings`, plus the segmented/annotated image
series via `segmented_SeriesInstanceUID` / `referenced_SeriesInstanceUID`; note that
`BodyPartExamined` reflects the source acquisition, NOT what a SEG/RTSTRUCT segments),
`ct_index` / `mr_index` / `pt_index` (acquisition parameters), `sm_index` / `sm_instance_index`
(slide microscopy). Outside the naming convention: `contrast_index`, `volume_geometry_index`
(cross-modality), `clinical_index` (per-collection, joins on `collection_id`). This makes
relational questions answerable — e.g. "pathology slides (Modality='SM') with a segmentation of
structure X" is
`index JOIN seg_index ON seg_index.segmented_SeriesInstanceUID = index.SeriesInstanceUID`,
filtered with `list_contains(seg_index.SegmentedPropertyType_CodeMeanings, 'X')`. Columns typed
`STRING[]` (e.g. the `*_CodeMeanings` columns) are arrays — match elements with
`list_contains(col, 'value')`, not `=` or `LIKE`. Call `get_table_schema(table)` for
exact columns. Still BigQuery-only: per-individual-segment detail, SR radiomics measurements,
and private DICOM elements — point the user to `idc-index` + BigQuery for those.

**Clinical (non-imaging) data** comes in two layers. `clinical_index` is a *dictionary*: one row
per (collection, table, column) with a human-readable `column_label` and an array of coded
`values` — use it to find *what* clinical attributes exist and what their codes mean. The actual
clinical rows live in per-collection tables (e.g. `nlst_canc`) registered under the `clinical`
schema, queried as `clinical.<table>` and joined to imaging on `dicom_patient_id =
index.PatientID` (NOT SeriesInstanceUID). Clinical data is not a filterable attribute and is not
harmonized across collections, so always discover with `list_clinical_tables` /
`get_clinical_table_schema` (or query `clinical_index`) before writing clinical SQL — e.g.
"NLST patients imaged with CT whose cancer is stage IV" is
`index JOIN clinical.nlst_canc ON index.PatientID = clinical.nlst_canc.dicom_patient_id`
filtered on the relevant staging column. Use `get_clinical_table` to read a whole small table.
These tables are present only when `clinical_index` is included in the build.
"""


@mcp.resource("idc://guide", mime_type="text/markdown")
def guide_resource() -> str:
    """How to query IDC with these tools (data model, recommended workflow)."""
    return _GUIDE


@mcp.resource("idc://tables", mime_type="application/json")
def tables_resource() -> str:
    """The tables available to run_sql, with descriptions and column counts."""
    return json.dumps(ctx.query.list_tables().model_dump(mode="json"), indent=2)


@mcp.resource("idc://schema/{table}", mime_type="application/json")
def schema_resource(table: str) -> str:
    """Full column schema (name, type, description) for a given IDC table."""
    if table not in ctx.backend.list_tables():
        raise ToolError(f"Unknown table: {table!r}")
    return json.dumps(core_schema.table_schema(table), indent=2)


# --- entrypoint ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="IDC MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable-http (hosted/shared) instead of stdio (local).",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if args.http:
        if args.host:
            mcp.settings.host = args.host
        if args.port:
            mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        # Local stdio mode: the server is on the user's machine, so enable real downloads.
        ctx.settings.enable_local_download = True
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
