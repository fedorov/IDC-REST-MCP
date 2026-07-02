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

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

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
    filters: CohortFilters = Field(default_factory=CohortFilters)
    page: int = 0
    page_size: int | None = None
    include_rows: bool = True


class ManifestTextRequest(BaseModel):
    filters: CohortFilters = Field(default_factory=CohortFilters)
    source: str = "aws"
    limit: int | None = None


class SqlRequest(BaseModel):
    sql: str
    max_rows: int | None = None


class CitationsRequest(BaseModel):
    filters: CohortFilters = Field(default_factory=CohortFilters)
    citation_format: str = "apa"


class DownloadRequest(BaseModel):
    download_dir: str
    collection_id: list[str] | None = None
    patientId: list[str] | None = None
    studyInstanceUID: list[str] | None = None
    seriesInstanceUID: list[str] | None = None
    dry_run: bool = False
    source_bucket_location: str = "aws"


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
        version="3.0.0",
        summary="LLM-first REST API for NCI Imaging Data Commons, backed by idc-index + DuckDB.",
        lifespan=lifespan,
    )
    if ctx is not None:
        app.state.ctx = ctx
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    @app.get("/", tags=["meta"])
    def root():
        return {
            "name": "IDC API",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "version_endpoint": f"{API_PREFIX}/version",
        }

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    # --- discovery ---
    @app.get(f"{API_PREFIX}/version", response_model=VersionInfo, tags=["discovery"])
    def version():
        return C().discovery.version()

    @app.get(f"{API_PREFIX}/stats", response_model=Stats, tags=["discovery"])
    def stats():
        return C().discovery.stats()

    @app.get(
        f"{API_PREFIX}/collections",
        response_model=list[CollectionSummary],
        tags=["discovery"],
    )
    def collections():
        return C().discovery.list_collections()

    @app.get(
        f"{API_PREFIX}/collections/{{collection_id}}",
        response_model=CollectionDetail,
        tags=["discovery"],
    )
    def collection(collection_id: str):
        return C().discovery.get_collection(collection_id)

    @app.get(
        f"{API_PREFIX}/analysis_results",
        response_model=list[AnalysisResult],
        tags=["discovery"],
    )
    def analysis_results():
        return C().discovery.list_analysis_results()

    @app.get(
        f"{API_PREFIX}/attributes", response_model=list[AttributeInfo], tags=["discovery"]
    )
    def attributes():
        return C().discovery.list_attributes()

    @app.get(
        f"{API_PREFIX}/attributes/{{attribute}}/values",
        response_model=AttributeValues,
        tags=["discovery"],
    )
    def attribute_values(attribute: str, limit: int = Query(100, ge=1, le=10000)):
        return C().discovery.get_attribute_values(attribute, limit=limit)

    # --- schema discovery ---
    @app.get(f"{API_PREFIX}/tables", response_model=TableList, tags=["query"])
    def tables():
        return C().query.list_tables()

    @app.get(f"{API_PREFIX}/tables/{{table}}", response_model=TableSchema, tags=["query"])
    def table_schema(table: str):
        return C().query.get_table_schema(table)

    # --- clinical data ---
    @app.get(
        f"{API_PREFIX}/clinical/tables", response_model=ClinicalTableList, tags=["clinical"]
    )
    def clinical_tables(collection_id: str | None = Query(None)):
        return C().clinical.list_clinical_tables(collection_id=collection_id)

    @app.get(
        f"{API_PREFIX}/clinical/tables/{{table}}",
        response_model=TableSchema,
        tags=["clinical"],
    )
    def clinical_table_schema(table: str):
        return C().clinical.get_clinical_table_schema(table)

    @app.get(
        f"{API_PREFIX}/clinical/tables/{{table}}/rows",
        response_model=SqlResult,
        tags=["clinical"],
    )
    def clinical_table_rows(table: str, max_rows: int | None = Query(None, ge=1, le=100000)):
        return C().clinical.get_clinical_table(table, max_rows=max_rows)

    # --- cohort / manifest ---
    @app.post(f"{API_PREFIX}/cohort/counts", response_model=CohortCounts, tags=["cohort"])
    def cohort_counts(filters: CohortFilters):
        return C().cohort.counts(filters)

    @app.post(
        f"{API_PREFIX}/cohort/manifest", response_model=ManifestResponse, tags=["cohort"]
    )
    def cohort_manifest(req: ManifestRequest):
        return C().cohort.build_manifest(
            req.filters, page=req.page, page_size=req.page_size, include_rows=req.include_rows
        )

    @app.post(f"{API_PREFIX}/cohort/manifest.txt", tags=["cohort"])
    def cohort_manifest_text(req: ManifestTextRequest):
        text = C().manifest.manifest_text(req.filters, source=req.source, limit=req.limit)
        return PlainTextResponse(text)

    # --- guarded SQL ---
    @app.post(f"{API_PREFIX}/sql", response_model=SqlResult, tags=["query"])
    def sql(req: SqlRequest):
        return C().query.run_sql(req.sql, max_rows=req.max_rows)

    # --- viewer / citations / licenses ---
    @app.get(f"{API_PREFIX}/viewer-url", response_model=ViewerURL, tags=["tools"])
    def viewer_url(
        series_instance_uid: str | None = None,
        study_instance_uid: str | None = None,
        viewer: str | None = None,
    ):
        return C().viewer.get_viewer_url(
            series_instance_uid=series_instance_uid,
            study_instance_uid=study_instance_uid,
            viewer=viewer,
        )

    @app.post(f"{API_PREFIX}/citations", response_model=CitationsResult, tags=["tools"])
    def citations(req: CitationsRequest):
        return C().citations.get_citations(req.filters, citation_format=req.citation_format)

    @app.post(f"{API_PREFIX}/licenses", response_model=LicensesResult, tags=["tools"])
    def licenses(filters: CohortFilters):
        return C().licenses.get_licenses(filters)

    # --- download (local mode only) ---
    @app.post(f"{API_PREFIX}/download", tags=["tools"])
    def download(req: DownloadRequest):
        return C().download.download(
            download_dir=req.download_dir,
            collection_id=req.collection_id,
            patientId=req.patientId,
            studyInstanceUID=req.studyInstanceUID,
            seriesInstanceUID=req.seriesInstanceUID,
            dry_run=req.dry_run,
            source_bucket_location=req.source_bucket_location,
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
