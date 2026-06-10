"""FastAPI application for IDC API v3.

Routes are deliberately thin: each one validates input via Pydantic and delegates to a core
service. No business logic or SQL lives here — that all sits in ``idc_api.core``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
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
        title="IDC API v3",
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

    @app.exception_handler(IDCAPIError)
    async def _idc_error_handler(_request, exc: IDCAPIError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    def C() -> AppContext:
        return app.state.ctx

    # --- meta ---
    @app.get("/", tags=["meta"])
    def root():
        return {
            "name": "IDC API v3",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "version_endpoint": f"{API_PREFIX}/version",
        }

    @app.get("/healthz", tags=["meta"])
    def healthz():
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
