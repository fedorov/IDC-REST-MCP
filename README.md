# IDC API v3

[![v3 CI](https://github.com/ImagingDataCommons/IDC-API/actions/workflows/v3-ci.yml/badge.svg)](https://github.com/ImagingDataCommons/IDC-API/actions/workflows/v3-ci.yml)

LLM-first **REST API** and **MCP server** for the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/),
backed by the [`idc-index`](https://github.com/ImagingDataCommons/idc-index) Parquet index
queried locally with DuckDB. All IDC data is open — **no authentication required**.

One backend-agnostic **core** library, two thin adapters over it:

- **REST API** (FastAPI) — language-agnostic HTTP access with auto-generated OpenAPI docs.
- **MCP server** — the same capabilities as Model Context Protocol tools, so LLM agents
  (Claude, etc.) can query IDC directly.

Both surfaces share one core, so a capability is implemented and tested once and exposed in
both.

> **Status:** Phase 1 (MVP). Core radiology discovery, cohort/manifest building, guarded
> read-only SQL, schema discovery, viewer URLs, citations, and licenses. Specialized indices
> (ct/mr/pt, seg/ann, slide microscopy), clinical data, and an optional BigQuery backend are
> planned for later phases.

## Documentation

- **[User Guide](docs/user-guide.md)** — concepts, the query surfaces and how they relate, the
  recommended workflow, and worked REST/MCP examples. **Start here to learn how to use it.**
- [`dev/architecture.md`](dev/architecture.md) — internal design (one core, two adapters).
- [`dev/api_v3_plan.md`](dev/api_v3_plan.md) — full design rationale + SQL threat model.
- [`dev/deployment.md`](dev/deployment.md) — Cloud Run deployment.
- [`dev/developer_guide.md`](dev/developer_guide.md) — contributing / adding capabilities.

## Install (development)

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --directory . pytest tests_v3 -q
```

The first run builds a small read-only DuckDB database from the bundled `idc-index-data`
Parquet (cached under your temp dir, pinned to the index version). It is rebuilt only when
`idc-index-data` is upgraded. No GCP account, network, or credentials are needed for queries.

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
docker build -f Dockerfile.v3 -t idc-api-v3 .
docker run -p 8080:8080 idc-api-v3            # REST API
```

The image bakes the read-only DuckDB file (set via `IDC_API_DUCKDB_PATH`) at build time, so
cold starts are instant and the container is fully stateless (no secrets, no GCP data access).
Rebuild on each IDC release to pick up the new `idc-index-data`.

**Cloud Run:** step-by-step instructions (build/push, deploy, the DuckDB-memory sizing rule,
updating IDC versions, and the optional hosted MCP service) are in
[`dev/deployment.md`](dev/deployment.md).
