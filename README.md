# IDC API

[![CI](https://github.com/fedorov/IDC-REST-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/fedorov/IDC-REST-MCP/actions/workflows/ci.yml)

LLM-first **REST API** and **MCP server** for the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/),
backed by the [`idc-index`](https://github.com/ImagingDataCommons/idc-index) Parquet index
queried locally with DuckDB. All IDC data is open — **no authentication required**.

One backend-agnostic **core** library, two thin adapters over it:

- **REST API** (FastAPI) — language-agnostic HTTP access with auto-generated OpenAPI docs.
- **MCP server** — the same capabilities as Model Context Protocol tools, so LLM agents
  (Claude, etc.) can query IDC directly.

Both surfaces share one core, so a capability is implemented and tested once and exposed in
both.

> **Status:** Phase 1 (MVP). Discovery, cohort/manifest building, guarded read-only SQL, schema
> discovery, viewer URLs, citations, and licenses. SQL can query and join the specialized
> indices (seg/ann/rtstruct, ct/mr/pt, slide microscopy, contrast/geometry, clinical) — fetched
> at build time. An optional BigQuery backend (for per-segment detail, SR radiomics, and private
> DICOM elements) is planned for a later phase.

## Documentation

- **[User Guide](docs/user-guide.md)** — concepts, the query surfaces and how they relate, the
  recommended workflow, and worked REST/MCP examples. **Start here to learn how to use it.**
- [`dev/architecture.md`](dev/architecture.md) — internal design (one core, two adapters).
- [`dev/api_v3_plan.md`](dev/api_v3_plan.md) — full design rationale + SQL threat model.
- [`dev/deployment.md`](dev/deployment.md) — Cloud Run deployment.
- [`dev/developer_guide.md`](dev/developer_guide.md) — contributing / adding capabilities.
- [`SECURITY.md`](SECURITY.md) — threat model, hardening in place, and how to report a
  vulnerability.

## Install (development)

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --directory . pytest tests -q
```

The first run builds a read-only DuckDB database (cached under your temp dir, pinned to the
index version; rebuilt only when `idc-index-data` is upgraded). By default it also fetches the
specialized indices from idc-index releases (~40 MB), so the **first build downloads data** —
set `IDC_API_INCLUDE_INDICES=none` to build from the bundled Parquet only (no build-time
downloads, just what `idc-index-data` already ships). No GCP account or credentials are ever
needed.

## Run

```bash
uv run idc-api                                       # REST API → http://127.0.0.1:8000 (Swagger at /docs)
uv run idc-mcp                                       # MCP server, stdio (local) — can also download files
uv run idc-mcp --http --host 0.0.0.0 --port 8080     # MCP server, hosted/shared (manifests only)
```

Quick taste — build a breast-MRI cohort over REST:

```bash
curl -s localhost:8000/v3/cohort/manifest \
  -H 'content-type: application/json' \
  -d '{"filters": {"terms": {"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}}, "page_size": 3}'
```

For the endpoint/tool reference, MCP client setup, downloads, and configuration, see the
**[User Guide](docs/user-guide.md)**.

## Deploy

Run it anywhere with the slim image:

```bash
docker build -t idc-api .
docker run -p 8080:8080 idc-api               # REST API
```

The image bakes the read-only DuckDB file (set via `IDC_API_DUCKDB_PATH`) at build time, so
cold starts are instant and the container is fully stateless (no secrets, no GCP data access).
Rebuild on each IDC release to pick up the new `idc-index-data`.

**Cloud Run:** step-by-step instructions (build/push, deploy, the DuckDB-memory sizing rule,
updating IDC versions, and the optional hosted MCP service) are in
[`dev/deployment.md`](dev/deployment.md).
