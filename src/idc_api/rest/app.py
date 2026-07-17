"""FastAPI application for the IDC API.

Routes are deliberately thin: each one validates input via Pydantic and delegates to a core
service. No business logic or SQL lives here — that all sits in ``idc_api.core``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.context import AppContext, get_context
from ..core.errors import IDCAPIError
from ..core.models import (
    AnalysisResult,
    AttributeInfo,
    AttributeValues,
    CitationsResult,
    ClinicalTableList,
    CohortCounts,
    CohortFilters,
    CollectionDetail,
    CollectionSummary,
    LicensesResult,
    ManifestResponse,
    SqlResult,
    Stats,
    TableList,
    TableSchema,
    VersionInfo,
    ViewerURL,
)
from ..core.version import server_version
from ..http_headers import HSTSMiddleware
from ..settings import get_settings

API_PREFIX = "/v3"
_SQL_PATH = f"{API_PREFIX}/sql"

logger = logging.getLogger("idc_api.rest")


def _format_sql(sql: str, settings) -> str:
    """Render `sql` for the audit log per IDC_API_SQL_LOG_MODE: a capped readable snippet
    (default), or a short digest that lets callers correlate repeated identical queries without
    putting query text in logs at all."""
    if settings.sql_log_mode == "hash":
        return "sha256:" + hashlib.sha256(sql.encode()).hexdigest()[:12]
    return sql[: settings.sql_log_chars]


# --- request bodies (response models are the shared core models) --------------------------


class ManifestRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "filters": {"terms": {"collection_id": ["nlst"], "Modality": ["CT"]}},
                    "page": 0,
                    "page_size": 50,
                    "include_rows": True,
                }
            ]
        }
    )

    filters: CohortFilters = Field(default_factory=CohortFilters)
    page: int = 0
    page_size: int | None = None
    include_rows: bool = True


class ManifestTextRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"filters": {"terms": {"collection_id": ["nlst"]}}, "source": "gcs", "limit": 1000}
            ]
        }
    )

    filters: CohortFilters = Field(default_factory=CohortFilters)
    source: str = "aws"
    limit: int | None = None


class SqlRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "sql": "SELECT Modality, count(*) AS n FROM index GROUP BY 1 ORDER BY n DESC",
                    "max_rows": 20,
                }
            ]
        }
    )

    sql: str
    max_rows: int | None = None


class CitationsRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"filters": {"terms": {"collection_id": ["nlst"]}}, "citation_format": "apa"}
            ]
        }
    )

    filters: CohortFilters = Field(default_factory=CohortFilters)
    citation_format: str = "apa"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the DuckDB backend at startup so the first request is fast.
    app.state.ctx = get_context()
    yield
    app.state.ctx.close()


def create_app(ctx: AppContext | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IDC API",
        # Driven by the installed package (+ IDC_API_BUILD stamp) via the shared helper, not a
        # hardcoded literal, so /v3/openapi.json reflects the actual build.
        version=server_version(),
        summary="LLM-first REST API for NCI Imaging Data Commons, backed by idc-index + DuckDB.",
        lifespan=lifespan,
        # Every versioned route — schema, docs, and health included — lives under the API_PREFIX
        # (/v3), so a new major version can be served side by side without moving anything. The
        # only route outside it is the bare-root redirect below. See dev/deployment.md
        # "Shared-domain path routing".
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
    )
    if ctx is not None:
        app.state.ctx = ctx
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # HSTS must stay *outside* CORS (added after it — later-added middleware wraps earlier), so
    # the header also lands on CORS preflights, which CORSMiddleware answers without calling
    # inward. The audit middleware below is added later still and ends up outermost; that's
    # fine — it only logs and passes every response through. Guarded by a test that asserts
    # the header on a preflight response.
    if settings.hsts_max_age > 0:
        app.add_middleware(HSTSMiddleware, max_age=settings.hsts_max_age)

    @app.middleware("http")
    async def _audit_log(request: Request, call_next):
        # One structured line per request: path/status/duration, plus the SQL (rendered per
        # IDC_API_SQL_LOG_MODE) for the guarded SQL endpoint. No query params or client IP —
        # Cloud Run's own request log already has the caller IP, correlatable by timestamp.
        # Reading the body here is safe: Starlette caches it, so the route handler's own
        # Pydantic parsing below reuses the cached bytes instead of re-reading the stream.
        start = time.monotonic()
        entry = {"path": request.url.path, "method": request.method}
        if request.url.path == _SQL_PATH and request.method == "POST":
            try:
                sql = json.loads(await request.body()).get("sql")
                if isinstance(sql, str):
                    entry["sql"] = _format_sql(sql, settings)
            except Exception:  # nosec B110 - malformed body; the route's own validation reports it
                pass
        try:
            response = await call_next(request)
        except Exception:
            entry["status"] = 500
            entry["error"] = "unhandled"
            raise
        else:
            entry["status"] = response.status_code
            return response
        finally:
            entry["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
            logger.info(json.dumps(entry))

    @app.exception_handler(IDCAPIError)
    async def _idc_error_handler(_request, exc: IDCAPIError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    def C() -> AppContext:
        return app.state.ctx

    # --- meta ---
    @app.get("/", include_in_schema=False)
    def root_redirect():
        """Send the bare domain to the interactive docs.

        The hosting load balancer routes unmatched paths to this service, so `/` lands here
        rather than 404ing; bare-domain traffic on an API host is overwhelmingly a human in a
        browser, and `/v3` answers with JSON. Deliberately an *exact* match on `/` and not a
        catch-all: every other unmatched path must keep 404ing, or the `/.well-known/…` probes
        MCP clients make during auth discovery would answer 200 HTML and be misread as auth
        metadata. 307, not 301 — the target is version-numbered, and a permanent redirect would
        be cached past the life of /v3.
        """
        return RedirectResponse(f"{API_PREFIX}/docs", status_code=307)

    @app.get(API_PREFIX, tags=["meta"], summary="API root")
    def root():
        """Entry point for the API: returns the server and build version, and links to the
        interactive docs, the OpenAPI schema, and the version endpoint."""
        return {
            "name": "IDC API",
            "server_version": server_version(),  # this server's software/build version
            "docs": f"{API_PREFIX}/docs",
            "openapi": f"{API_PREFIX}/openapi.json",
            "version_endpoint": f"{API_PREFIX}/version",
        }

    @app.get(f"{API_PREFIX}/health", tags=["meta"], summary="Health check")
    def health():
        """Liveness probe for the load balancer and uptime checks. Returns `{"status": "ok"}`
        once the service is up."""
        return {"status": "ok"}

    # --- discovery ---
    @app.get(
        f"{API_PREFIX}/version",
        response_model=VersionInfo,
        tags=["discovery"],
        summary="IDC and server version",
        responses={
            200: {
                "content": {
                    "application/json": {
                        "examples": {
                            "illustrative": {
                                "summary": "Illustrative — not live values",
                                "value": {
                                    "idc_version": "v24",
                                    "idc_index_data_version": "24.0.0",
                                    "api_version": "3.0.0",
                                    "build": "a1b2c3d",
                                },
                            }
                        }
                    }
                }
            }
        },
    )
    def version():
        """Report the IDC data release served (e.g. `v24`) and the pinned idc-index-data
        version, plus this server's own software `api_version` (and `build` stamp, if the deploy
        set one). Use it to confirm which IDC version — and which build of this server —
        produced a given result."""
        return C().discovery.version()

    @app.get(
        f"{API_PREFIX}/stats",
        response_model=Stats,
        tags=["discovery"],
        summary="Headline totals",
        responses={
            200: {
                "content": {
                    "application/json": {
                        "examples": {
                            "illustrative": {
                                "summary": "Illustrative — not live values",
                                "value": {
                                    "idc_version": "v24",
                                    "collections": 187,
                                    "analysis_results": 42,
                                    "patients": 68000,
                                    "studies": 130000,
                                    "series": 1500000,
                                    "instances": 55000000,
                                    "size_TB": 112.5,
                                },
                            }
                        }
                    }
                }
            }
        },
    )
    def stats():
        """Headline totals for all of IDC: the number of collections, analysis results,
        patients, studies, series, and instances, plus the total size in TB."""
        return C().discovery.stats()

    @app.get(
        f"{API_PREFIX}/collections",
        response_model=list[CollectionSummary],
        tags=["discovery"],
        summary="List collections",
    )
    def collections():
        """List all IDC collections (original imaging datasets) with cancer types, tumor
        locations, species, and subject counts. Use it to find a `collection_id` to filter
        on."""
        return C().discovery.list_collections()

    @app.get(
        f"{API_PREFIX}/collections/{{collection_id}}",
        response_model=CollectionDetail,
        tags=["discovery"],
        summary="Collection detail",
    )
    def collection(collection_id: str = Path(..., examples=["nlst"])):
        """Detailed metadata for one collection: description, subject/series/instance counts,
        total size, the modalities present, and the license breakdown."""
        return C().discovery.get_collection(collection_id)

    @app.get(
        f"{API_PREFIX}/analysis_results",
        response_model=list[AnalysisResult],
        tags=["discovery"],
        summary="List analysis results",
    )
    def analysis_results():
        """List IDC analysis results — derived datasets (AI or expert segmentations,
        annotations, radiomics) layered on the original collections. Use it to find an
        `analysis_result_id`."""
        return C().discovery.list_analysis_results()

    @app.get(
        f"{API_PREFIX}/attributes",
        response_model=list[AttributeInfo],
        tags=["discovery"],
        summary="List filter attributes",
    )
    def attributes():
        """List the attributes a cohort can be filtered by (name, type, whether categorical).
        Use it to learn valid filter attribute names before building a cohort. These are a
        curated subset of the `index` table chosen for cohort filtering — `/sql` can query or
        filter on any column in any table listed by `/tables`, including `index` columns that
        aren't filter attributes."""
        return C().discovery.list_attributes()

    @app.get(
        f"{API_PREFIX}/attributes/{{attribute}}/values",
        response_model=AttributeValues,
        tags=["discovery"],
        summary="Distinct attribute values",
    )
    def attribute_values(
        attribute: str = Path(..., examples=["Modality"]),
        limit: int = Query(100, ge=1, le=10000, examples=[10]),
    ):
        """Return the distinct values (with counts) of a categorical attribute on the `index`
        table, e.g. `Modality` or `BodyPartExamined`. Query this before filtering by an attribute
        so you use real values with the correct casing rather than guessing. The response carries
        a `truncated` flag: when `false` the list is complete; when `true`, raise `limit` (capped
        server-side) and re-check."""
        return C().discovery.get_attribute_values(attribute, limit=limit)

    # --- schema discovery ---
    @app.get(
        f"{API_PREFIX}/tables",
        response_model=TableList,
        tags=["query"],
        summary="List queryable tables",
    )
    def tables():
        """List the tables available to the SQL endpoint: the main `index`, the collection,
        analysis, and version metadata tables, and the specialized indices — each named
        `<modality>_index` after the DICOM Modality it describes (`seg_index`: segmented anatomy
        of SEG series; `ct_index`, `mr_index`, `pt_index`: acquisition parameters; `sm_index`,
        `ann_index`: microscopy), plus `contrast_index`, `volume_geometry_index`, and
        `clinical_index`. Consult it before writing SQL, and whenever a property you need (e.g.
        what a segmentation contains) is not a filterable attribute — it may live in a
        specialized index. Per-collection clinical tables are listed separately by
        `/clinical/tables`."""
        return C().query.list_tables()

    @app.get(
        f"{API_PREFIX}/tables/{{table}}",
        response_model=TableSchema,
        tags=["query"],
        summary="Table schema",
    )
    def table_schema(table: str = Path(..., examples=["index"])):
        """Return the columns (name, type, description) of a table. Use it to get correct column
        names before querying `/sql`. Pass `index` for the main series-level table."""
        return C().query.get_table_schema(table)

    # --- clinical data ---
    @app.get(
        f"{API_PREFIX}/clinical/tables",
        response_model=ClinicalTableList,
        tags=["clinical"],
        summary="List clinical tables",
    )
    def clinical_tables(collection_id: str | None = Query(None, examples=["nlst"])):
        """Discover the per-collection clinical (non-imaging) data tables — demographics,
        diagnoses, cancer staging, therapies, labs, outcomes. Clinical data is not a filterable
        attribute and is not harmonized across collections, so table and column names vary per
        collection. Pass `collection_id` to narrow to one collection. Each table is queryable via
        `/sql` as `clinical.<table_name>` and joins to `index` on
        `dicom_patient_id = index.PatientID`."""
        return C().clinical.list_clinical_tables(collection_id=collection_id)

    @app.get(
        f"{API_PREFIX}/clinical/tables/{{table}}",
        response_model=TableSchema,
        tags=["clinical"],
        summary="Clinical table schema",
    )
    def clinical_table_schema(table: str = Path(..., examples=["nlst_canc"])):
        """Return the columns of a clinical table (name, DuckDB type, and a human-readable label
        from `clinical_index`, since clinical column names are often cryptic). Get the table name
        from `/clinical/tables`."""
        return C().clinical.get_clinical_table_schema(table)

    @app.get(
        f"{API_PREFIX}/clinical/tables/{{table}}/rows",
        response_model=SqlResult,
        tags=["clinical"],
        summary="Read clinical table rows",
    )
    def clinical_table_rows(
        table: str = Path(..., examples=["nlst_canc"]),
        max_rows: int | None = Query(None, ge=1, le=100000, examples=[100]),
    ):
        """Return the rows of a clinical table (capped at `max_rows`). Use it to inspect a small
        clinical table directly; for filtering by clinical attributes or joining to imaging,
        query `/sql` against `clinical.<table>` instead. Get the table name from
        `/clinical/tables`."""
        return C().clinical.get_clinical_table(table, max_rows=max_rows)

    # --- cohort / manifest ---
    @app.post(
        f"{API_PREFIX}/cohort/counts",
        response_model=CohortCounts,
        tags=["cohort"],
        summary="Cohort counts",
    )
    def cohort_counts(filters: CohortFilters):
        """Return distinct counts for a filtered cohort — patients, studies, series, instances,
        and total `size_TB` — without the sample rows or download payload. Use it as a fast size
        check before building a full manifest or downloading. `terms` is `{attribute: [values]}`
        for equality/IN; `ranges` is `{attribute: {"gte": x, "lte": y}}` for numeric or date
        ranges."""
        return C().cohort.counts(filters)

    @app.post(
        f"{API_PREFIX}/cohort/manifest",
        response_model=ManifestResponse,
        tags=["cohort"],
        summary="Build cohort manifest",
    )
    def cohort_manifest(req: ManifestRequest):
        """Build a cohort from structured filters and get back distinct counts (patients,
        studies, series, instances, size_TB), a page of matching series, and a download payload
        (idc commands plus a manifest preview). `filters.terms` is `{attribute: [values]}` for
        equality/IN (e.g. `{"Modality": ["MR"]}`); `filters.ranges` is
        `{attribute: {"gte": x, "lte": y}}` for numeric or date ranges. Discover valid attributes
        via `/attributes` and valid values via `/attributes/{attribute}/values`. For anything
        these structured filters can't express, use `/sql`."""
        return C().cohort.build_manifest(
            req.filters, page=req.page, page_size=req.page_size, include_rows=req.include_rows
        )

    @app.post(
        f"{API_PREFIX}/cohort/manifest.txt",
        tags=["cohort"],
        summary="Cohort manifest (plain text)",
    )
    def cohort_manifest_text(req: ManifestTextRequest):
        """Return a plain-text manifest of public download URLs (one `s3://` per series,
        compatible with `idc download-from-manifest`) for a filtered cohort, up to `limit`
        lines. `source` is `aws` (default) or `gcs` — both use the `s3://` scheme (GCS is
        reached via its S3-compatible endpoint, matching idc-index). These are anonymous public
        URLs — feed the file to the `idc` CLI, or `s5cmd --no-sign-request` directly for
        `source=aws` (add `--endpoint-url https://storage.googleapis.com` for `source=gcs`). The
        response is `text/plain`, one URL per line."""
        text = C().manifest.manifest_text(req.filters, source=req.source, limit=req.limit)
        return PlainTextResponse(text)

    # --- guarded SQL ---
    @app.post(
        f"{API_PREFIX}/sql",
        response_model=SqlResult,
        tags=["query"],
        summary="Run read-only SQL",
    )
    def sql(req: SqlRequest):
        """Run a single read-only SQL `SELECT`/`WITH` against the IDC index (DuckDB) and return
        the rows. Use it for anything the cohort filters can't express — GROUP BY, joins across
        tables, custom aggregations, or filtering on columns that aren't filter attributes. The
        attributes from `/attributes` are a curated subset of `index`, so `/sql` is how you reach
        the rest: other `index` columns (e.g. `SeriesDescription`, `PatientAge`) and columns that
        live only in a specialized index (e.g. segmented anatomy in `seg_index`). The connection
        is sandboxed: no writes, no file or network access, one statement only. Get correct table
        and column names from `/tables` and `/tables/{table}` first; the main table is `index`,
        and per-collection clinical tables are in the `clinical` schema. The result carries a
        `truncated` flag — when `true` you did not get every row, so narrow or aggregate the
        query, or raise `max_rows` (clamped to a server ceiling) and re-check."""
        return C().query.run_sql(req.sql, max_rows=req.max_rows)

    # --- viewer / citations / licenses ---
    @app.get(
        f"{API_PREFIX}/viewer-url",
        response_model=ViewerURL,
        tags=["tools"],
        summary="Viewer URL",
    )
    def viewer_url(
        series_instance_uid: str | None = Query(
            None,
            examples=["1.2.840.113654.2.55.136638632728533399820524570150364784952"],
        ),
        study_instance_uid: str | None = Query(
            None,
            examples=["1.2.840.113654.2.55.100004988183996567551011427980805457777"],
        ),
        viewer: str | None = Query(None, examples=["ohif_v3"]),
    ):
        """Return a browser viewer URL (OHIF for radiology, Slim for slide microscopy) for a
        series or study, so images can be viewed without downloading. Provide a
        `series_instance_uid` or `study_instance_uid` (obtain one from a cohort manifest or
        `/sql`)."""
        return C().viewer.get_viewer_url(
            series_instance_uid=series_instance_uid,
            study_instance_uid=study_instance_uid,
            viewer=viewer,
        )

    @app.post(
        f"{API_PREFIX}/citations",
        response_model=CitationsResult,
        tags=["tools"],
        summary="Cohort citations",
    )
    def citations(req: CitationsRequest):
        """Return the publications to cite for a cohort: per-dataset citations (from the cohort's
        source DOIs) in `citations`, plus the IDC paper in `idc_acknowledgment`.
        `citation_format` is one of `apa`, `bibtex`, `csl-json`, `turtle`. When publishing
        results that use IDC data, include the per-dataset citations and acknowledge IDC itself
        (see the `recommendation` field)."""
        return C().citations.get_citations(req.filters, citation_format=req.citation_format)

    @app.post(
        f"{API_PREFIX}/licenses",
        response_model=LicensesResult,
        tags=["tools"],
        summary="Cohort license breakdown",
    )
    def licenses(filters: CohortFilters):
        """Return the license breakdown (series count and size per license) for a cohort. Use it
        to check whether the data is commercial-friendly (CC BY) or non-commercial only
        (CC BY-NC) before reuse."""
        return C().licenses.get_licenses(filters)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
