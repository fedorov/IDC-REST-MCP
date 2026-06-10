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
both. See [`dev/api_v3_plan.md`](dev/api_v3_plan.md) for the full design and rationale.

> **Status:** Phase 1 (MVP). Core radiology discovery, cohort/manifest building, guarded
> read-only SQL, schema discovery, viewer URLs, citations, and licenses. Specialized indices
> (ct/mr/pt, seg/ann, slide microscopy), clinical data, and an optional BigQuery backend are
> planned for later phases.

## Install (development)

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --directory . pytest tests_v3 -q
```

The first run builds a small read-only DuckDB database from the bundled `idc-index-data`
Parquet (cached under your temp dir, pinned to the index version). It is rebuilt only when
`idc-index-data` is upgraded. No GCP account, network, or credentials are needed for queries.

## REST API

```bash
uv run idc-api          # http://127.0.0.1:8000  (Swagger UI at /docs)
```

| Method & path | Purpose |
|---|---|
| `GET /v3/version`, `/v3/stats` | IDC version and headline totals |
| `GET /v3/collections`, `/v3/collections/{id}` | Collections (datasets) + detail |
| `GET /v3/analysis_results` | Derived datasets (segmentations/annotations) |
| `GET /v3/attributes`, `/v3/attributes/{attr}/values` | Filterable attributes + value discovery |
| `GET /v3/tables`, `/v3/tables/{table}` | Schema discovery for SQL |
| `POST /v3/cohort/counts` | Distinct counts for a filter (cheap) |
| `POST /v3/cohort/manifest` | Counts + page of series + download payload |
| `POST /v3/cohort/manifest.txt` | Full manifest as `text/plain` (s3:// or gs://) |
| `POST /v3/sql` | Guarded read-only SQL (DuckDB) |
| `GET /v3/viewer-url` | OHIF/SLIM viewer link for a study/series |
| `POST /v3/citations`, `POST /v3/licenses` | Citations / license breakdown for a cohort |
| `POST /v3/download` | Local download (501 unless enabled) |

Example — build a breast-MRI cohort:

```bash
curl -s localhost:8000/v3/cohort/manifest \
  -H 'content-type: application/json' \
  -d '{"filters": {"terms": {"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}}, "page_size": 3}'
```

## MCP server

```bash
uv run idc-mcp              # stdio (local) — can also download files
uv run idc-mcp --http --host 0.0.0.0 --port 8080   # hosted/shared (manifests only)
```

Tools: `get_idc_version`, `get_stats`, `list_collections`, `get_collection`,
`list_analysis_results`, `list_attributes`, `get_attribute_values`, `list_tables`,
`get_table_schema`, `build_cohort`, `run_sql`, `get_cohort_urls`, `get_viewer_url`,
`get_citations`, `get_licenses`, `download_cohort`. Resources: `idc://guide`, `idc://tables`,
`idc://schema/{table}`.

### Use it from Claude Desktop / Claude Code

Add to your MCP client config (runs the server locally over stdio):

```json
{
  "mcpServers": {
    "idc": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/IDC-API", "idc-mcp"]
    }
  }
}
```

Then ask, e.g.: *"Find breast MRI in IDC, show the counts and total size, and give me a
download command."* Inspect/debug the tools with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run idc-mcp
```

### Local vs hosted (downloads)

An MCP server can run **locally** (stdio, on your machine — it can write files, so
`download_cohort` actually fetches DICOM via `idc-index`/s5cmd) or **hosted** (HTTP, shared —
no filesystem access, so retrieval returns a manifest + public URLs + an `idc download`
command). Same tools, two behaviors.

## Guarded SQL — why it's safe

`run_sql` / `POST /v3/sql` accept arbitrary SQL, but the data is **public** (nothing secret)
and the DuckDB connection is opened **read-only** (nothing to modify), so the classic SQL
injection consequences don't apply. The connection is further hardened per DuckDB's
[Securing DuckDB](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview)
guide (external file/network access disabled, no extensions, memory/row/time caps,
configuration locked), and only single read-only `SELECT`/`WITH` statements are accepted.
Values we interpolate into curated queries are passed as bound parameters
([OWASP](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)).
See [`dev/api_v3_plan.md`](dev/api_v3_plan.md) for the full threat model and references.

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

## Configuration

Environment variables (prefix `IDC_API_`): `DUCKDB_PATH`, `SQL_MAX_ROWS` (5000),
`SQL_TIMEOUT_SECONDS` (30), `DEFAULT_PAGE_SIZE` (100), `MAX_PAGE_SIZE` (5000),
`MANIFEST_HARD_CAP` (100000), `ENABLE_LOCAL_DOWNLOAD` (false), `CORS_ALLOW_ORIGINS`,
`HOST`, `PORT`.
