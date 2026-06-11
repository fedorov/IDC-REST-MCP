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
import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from ..core import schema as core_schema
from ..core.context import AppContext
from ..core.errors import IDCAPIError
from ..core.models import CohortFilters, NumericRange
from ..settings import get_settings

logger = logging.getLogger("idc_api.mcp")

INSTRUCTIONS = """\
This server exposes the NCI Imaging Data Commons (IDC) — public cancer imaging (DICOM) data.
All data is open; no authentication is needed. Data model: collection_id (a dataset) and
analysis_result_id (derived annotations/segmentations) group the DICOM hierarchy
Patient → Study → Series. The main queryable table is `index` (one row per series).

Recommended workflow:
1. Ground your query first: use list_attributes + get_attribute_values to get *valid* filter
   values (e.g. real Modality / BodyPartExamined values), and list_tables / get_table_schema
   before writing SQL. Do not guess values.
2. Explore with build_cohort (structured filters) for common cases, or run_sql (read-only
   DuckDB SELECT over `index`) for anything complex.
3. Sizes are large (IDC is ~100 TB). Always check counts/size_TB and warn the user before
   suggesting a download. Use get_cohort_urls / the returned `idc` commands to download;
   download_cohort actually transfers files only when this server runs locally.
Cite data with get_citations and respect get_licenses (CC-BY vs CC-BY-NC)."""

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


mcp = FastMCP(
    "IDC (Imaging Data Commons)",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security(),
)
ctx = AppContext()


def guard(fn):
    """Convert core errors into clean MCP tool errors; never leak tracebacks."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IDCAPIError as exc:
            raise ToolError(exc.message) from None
        except ToolError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in MCP tool %s", fn.__name__)
            raise ToolError("Internal error while handling the request.") from None

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
    """Return the IDC data release served (e.g. 'v24') and the pinned index version. Call
    this to confirm which IDC version your answers are based on."""
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
    Call this before build_cohort to learn valid filter attribute names."""
    return [a.model_dump(mode="json") for a in ctx.discovery.list_attributes()]


@mcp.tool()
@guard
def get_attribute_values(attribute: str, limit: int = 50) -> dict:
    """Return the distinct values (with counts) of a categorical attribute on the `index`
    table, e.g. attribute='Modality' or 'BodyPartExamined'. ALWAYS call this before
    filtering by an attribute so you use real values and correct casing — do not guess."""
    return ctx.discovery.get_attribute_values(attribute, limit=limit).model_dump(mode="json")


# --- schema discovery ---------------------------------------------------------------------


@mcp.tool()
@guard
def list_tables() -> dict:
    """List the tables available to run_sql (the main `index` plus collection/analysis/
    version metadata tables). Call before writing SQL."""
    return ctx.query.list_tables().model_dump(mode="json")


@mcp.tool()
@guard
def get_table_schema(table: str) -> dict:
    """Return the columns (name, type, description) of a table. Call this before run_sql to
    use correct column names. Use table='index' for the main series-level table."""
    return ctx.query.get_table_schema(table).model_dump(mode="json")


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
    aggregations). Only a single read-only SELECT/WITH statement is allowed; the connection
    is sandboxed (no writes, no file/network access). Call list_tables / get_table_schema
    first to use correct table and column names. The main table is `index`."""
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
    """Get the publications to cite for a cohort (from its source DOIs plus the main IDC
    paper). `citation_format` is one of: apa, bibtex, csl-json, turtle. Always include these
    when the user publishes results using IDC data."""
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
Patient → Study → Series, with two grouping levels above: `collection_id` (a dataset, e.g.
`nlst`, `tcga_luad`) and `analysis_result_id` (derived annotations/segmentations). The main
table is `index` (one row per *series*). IDC is large (~100+ TB) — always check size before
suggesting a download.

**The tools form a few families that build on each other:**
- *Discovery* (`get_stats`, `list_collections`, `get_collection`, `list_analysis_results`,
  `list_attributes`, `get_attribute_values`) — what exists, and the *vocabulary* (attribute
  names + valid values) you filter on.
- *Cohort* (`build_cohort`) — turn a chosen combination of that vocabulary into distinct
  counts + a sample of series + a download payload.
- *Retrieval* (`get_cohort_urls`, `download_cohort`) — the download half: public URLs / files.
- *SQL* (`list_tables`, `get_table_schema`, `run_sql`) — the escape hatch for anything
  `build_cohort` can't express (GROUP BY, joins, custom aggregations).
- *Side tools* (`get_viewer_url`, `get_citations`, `get_licenses`) — view / cite / license-check
  a cohort.

Prefer `build_cohort` for common cases (structured, can't be malformed); reach for `run_sql`
only when it can't express your query. Discovery feeds Cohort; Cohort reuses Retrieval to build
its payload — so a typical request flows Discovery → Cohort → Retrieval, with SQL as a bypass.

**Recommended workflow:**
1. *Find data:* `list_collections` / `get_collection` (imaging datasets), `list_analysis_results`
   (derived annotations & segmentations).
2. *Ground filters (do this first to avoid wrong values):* `list_attributes` → valid attributes;
   `get_attribute_values(attribute=...)` → valid values + counts (correct casing!).
3. *Build:* `build_cohort(terms={...}, ranges={...})` → counts, sample series, download payload.
   For complex queries: `list_tables` → `get_table_schema('index')` → `run_sql('SELECT ...')`.
4. *Get the data:* `get_cohort_urls` returns public s3:///gs:// URLs; the `build_cohort`
   response also includes ready-to-run `idc` CLI commands. `download_cohort` performs a real
   local download only when the server runs on your machine.
5. *Be a good citizen:* check `get_licenses` (CC BY vs CC BY-NC) and include `get_citations`
   output when publishing.
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
    if table not in core_schema.list_table_names():
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
