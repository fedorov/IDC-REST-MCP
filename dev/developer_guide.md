# IDC API — Developer Guide

How to set up, run, test, and extend the codebase. For the *why* see
[`dev/api_v3_plan.md`](api_v3_plan.md); for the *shape* see
[`dev/architecture.md`](architecture.md). User-facing docs are in
[`README.md`](../README.md).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (package/venv manager)
- Python 3.12 available to uv (3.11+ supported). No GCP account, network, or credentials are
  needed for queries — everything runs on the bundled `idc-index` Parquet.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

The first query builds a small read-only DuckDB file from the bundled `idc-index-data` Parquet
into your temp dir (cached, pinned to the index version; rebuilt only on `idc-index-data`
upgrade).

## Run

```bash
uv run idc-api                      # REST API → http://127.0.0.1:8000  (Swagger at /docs)
uv run idc-mcp                      # MCP server over stdio (local; download enabled)
uv run idc-mcp --http --port 8080   # MCP over streamable-http (hosted; download disabled)
```

Inspect the MCP tools interactively:

```bash
npx @modelcontextprotocol/inspector uv run idc-mcp
```

## Test

```bash
uv run --directory . pytest tests -q       # full suite (first run fetches specialized indices)
uv run --directory . pytest tests/test_backend_guards.py -q   # one file
```

> `uv run` discovers the project from the working directory. If your shell isn't at the repo
> root, pass `--directory /path/to/repo`.

| Test file | Covers |
|---|---|
| [test_backend_guards.py](../tests/test_backend_guards.py) | SQL sandbox: read-only, external access blocked, single-statement, row cap, timeout |
| [test_services_golden.py](../tests/test_services_golden.py) | **Golden:** results equal `idc-index` (IDCClient) on the same Parquet |
| [test_rest.py](../tests/test_rest.py) | REST endpoint shapes, 404/501 mapping, SQL guard, OpenAPI |
| [test_mcp.py](../tests/test_mcp.py) | Tools registered, prescriptive descriptions, calls, clean errors, resources |
| [test_parity.py](../tests/test_parity.py) | **Parity:** core service == REST == MCP for the same filter |
| [test_specialized_indices.py](../tests/test_specialized_indices.py) | Specialized indices fetched + exposed to SQL + joinable to `index` |

Fixtures live in [tests/conftest.py](../tests/conftest.py): `ctx` (the core
`AppContext`), `client` (FastAPI `TestClient`), and `parse_mcp` (normalizes a FastMCP
`call_tool` return into plain Python).

### Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on pushes to `main`
(and manual dispatch) and on PRs that touch `src/idc_api/**`, `tests/**`, `pyproject.toml`,
or `uv.lock`. It installs locked deps with `uv sync --extra dev`, then runs `ruff check` and
`pytest tests` on Python 3.11 and 3.12. CI needs no secrets or GCP; its first test run
downloads the specialized indices (`IDC_API_INCLUDE_INDICES=all` by default — set `none` to run
from bundled data only). Before pushing, run the same two commands locally:

```bash
uv run ruff check src tests
uv run pytest tests -q
```

## Project layout

```
src/idc_api/
  settings.py            # env-driven config (prefix IDC_API_)
  core/
    context.py           # AppContext: builds backend + services; get_context() singleton
    errors.py            # typed IDCAPIError subclasses (code + HTTP status)
    schema.py            # table registry, column metadata, FILTERABLE_ATTRIBUTES
    filters.py           # CohortFilters -> parameterized WHERE
    models.py            # Pydantic request/response models (the shared contract)
    backend/
      base.py            # QueryBackend interface
      duckdb_backend.py  # read-only DuckDB over idc-index Parquet
    services/            # discovery, cohort, query, manifest, viewer, citations, licenses, download
  rest/app.py            # FastAPI app + routes
  mcp/server.py          # FastMCP tools + resources + entrypoint
tests/                   # pytest suite
```

## Conventions (please keep these)

1. **`core/` never imports an adapter.** No `fastapi`/`mcp` imports under `core/`. Adapters
   import `core/`.
2. **Adapters are thin.** A route or tool validates input and calls a service. No SQL or domain
   logic in `rest/` or `mcp/`.
3. **Services return Pydantic models** from [models.py](../src/idc_api/core/models.py) — never
   raw dicts or DataFrames. Both adapters serialize the same models (this is what parity tests
   guarantee).
4. **SQL we author is parameterized.** Use `backend.query(sql, params)` with `?` placeholders;
   never f-string user *values* into SQL. Identifiers (table/column names) that can't be bound
   must be validated against `schema` allow-lists and double-quoted (see
   [filters.py](../src/idc_api/core/filters.py) and `DiscoveryService.get_attribute_values`).
5. **Raw caller SQL only via `backend.run_user_sql`.** Never route untrusted SQL through
   `backend.query`.
6. **MCP tool descriptions are prescriptive** about *when* to call the tool (e.g. "call this
   before filtering"), not just what it does — this measurably improves tool selection.
7. **Errors:** raise an [`IDCAPIError`](../src/idc_api/core/errors.py) subclass from services.
   REST maps it to `{status, code, message}`; the MCP `guard` decorator converts it to a clean
   `ToolError`. Never leak tracebacks.

## Walkthrough: add a new capability

Say you want `get_modality_summary()` (series count + size per modality). Touch five places:

1. **Model** — add to [models.py](../src/idc_api/core/models.py):
   ```python
   class ModalitySummaryItem(BaseModel):
       Modality: str | None
       series: int
       size_TB: float
   ```
2. **Service** — add a method to the relevant service (here `DiscoveryService`):
   ```python
   def modality_summary(self) -> list[ModalitySummaryItem]:
       rows = self.backend.query(
           "SELECT Modality, count(DISTINCT SeriesInstanceUID) series, "
           "COALESCE(sum(series_size_MB),0)/1000000 size_TB FROM index "
           "GROUP BY 1 ORDER BY series DESC"
       ).rows
       return [ModalitySummaryItem(**r) for r in rows]
   ```
3. **REST route** — in [rest/app.py](../src/idc_api/rest/app.py):
   ```python
   @app.get(f"{API_PREFIX}/modalities", response_model=list[ModalitySummaryItem], tags=["discovery"])
   def modalities():
       return C().discovery.modality_summary()
   ```
4. **MCP tool** — in [mcp/server.py](../src/idc_api/mcp/server.py):
   ```python
   @mcp.tool()
   @guard
   def get_modality_summary() -> list[dict]:
       """Series count and total size for each imaging Modality across all of IDC. Use to see
       what imaging types exist and how much there is."""
       return [m.model_dump(mode="json") for m in ctx.discovery.modality_summary()]
   ```
5. **Test** — add a parity check (core == REST == MCP) in `tests/`.

Run `uv run --directory . pytest tests -q` and you're done.

## Walkthrough: specialized index tables

Bundled tables (`schema.BUNDLED_TABLES`) ship as Parquet inside `idc-index-data`. The
specialized indices (`schema.SPECIALIZED_TABLES`: ct/mr/pt, seg/ann, sm, clinical, …) are
*fetched* from idc-index releases at build time and built into the DuckDB file:
`build_database_file` downloads each via `idc-index`'s `fetch_index` and `CREATE TABLE`s it,
gated by `IDC_API_INCLUDE_INDICES` (default `all`). Schema discovery (`list_tables` /
`get_table_schema`) and `run_sql` pick them up automatically — `backend.list_tables()` reads the
actual DuckDB catalog, so the listing reflects exactly what a given build included.

To expose a *new* index that idc-index adds later: add its name to `schema.SPECIALIZED_TABLES`
(the SQL name equals its `idc_index_data.INDEX_METADATA` key). Nothing else is required for SQL
access. Optionally add targeted service methods/tools for common joins (e.g. CT acquisition
parameters).

**Clinical data tables are a special case.** Fetching `clinical_index` also downloads ~150
per-collection clinical *data* tables (e.g. `nlst_canc`). These have no `INDEX_METADATA` schema
and would swamp `list_tables`, so `build_database_file` registers them under a dedicated
`clinical` schema (`schema.CLINICAL_SCHEMA`) via `_register_clinical_tables`, *excluded* from
`backend.list_tables()` (which reads only the `main` schema) and surfaced separately by
`backend.list_clinical_tables()`. `ClinicalService` drives discovery from `clinical_index` and
reads tables as `clinical.<table>`; they join to `index` on `dicom_patient_id = PatientID`. When
the build shape changes like this, bump `duckdb_backend._BUILD_REVISION` so stale cache files
aren't reused.

## Walkthrough: add the BigQuery backend (Phase 3)

1. Create `core/backend/bigquery_backend.py` implementing `QueryBackend` (`list_tables`,
   `query`, `run_user_sql`) against `bigquery-public-data.idc_current.*`.
2. Select it in [`AppContext`](../src/idc_api/core/context.py) (e.g. by a settings flag), or
   compose a router backend that falls back to BigQuery for columns the local index lacks.
3. Services and adapters are untouched — that's the point of the interface.

## Configuration

Environment variables (prefix `IDC_API_`), defined in
[settings.py](../src/idc_api/settings.py):

| Var | Default | Meaning |
|---|---|---|
| `DUCKDB_PATH` | (built at runtime) | Use a prebuilt read-only DuckDB file (image bakes one) |
| `INCLUDE_INDICES` | all | Specialized indices to fetch+build: `all`, `none`, or a comma list. Ignored when `DUCKDB_PATH` is set |
| `DUCKDB_MEMORY_LIMIT` / `DUCKDB_THREADS` / `DUCKDB_TEMP_DIRECTORY_SIZE` | 4GB / 4 / 4GB | Engine caps |
| `SQL_MAX_ROWS` | 5000 | Row cap for `run_sql` |
| `SQL_TIMEOUT_SECONDS` | 30 | Statement timeout |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | 100 / 5000 | Manifest paging |
| `MANIFEST_HARD_CAP` | 100000 | Max series a single manifest enumerates |
| `ENABLE_LOCAL_DOWNLOAD` | false | Allow real downloads (stdio MCP sets this) |
| `CORS_ALLOW_ORIGINS` / `HOST` / `PORT` | `["*"]` / 127.0.0.1 / 8000 | REST serving |

## Pitfalls & gotchas

- **DuckDB shares one instance per file path within a process.** Hardening is applied at
  connect-time via the `config` dict (not post-connect `SET`) so multiple backends in one
  process don't collide on `lock_configuration`. See `_hardening_config()`.
- **DuckDB connections aren't thread-safe.** Always run on a per-request `con.cursor()`. Don't
  reuse `IDCClient`'s connection for serving.
- **`INDEX_METADATA[...]["parquet_filepath"]` is a `Path`, not a `str`.** `schema.parquet_path`
  coerces it (DuckDB param binding rejects `Path`).
- **Citations make live DOI network calls** (`dx.doi.org`), so they're excluded from the
  offline suite — add a recorded-fixture test if you change that path.
- **The main table is `index`** (DuckDB name), even though its `INDEX_METADATA` key is
  `idc_index`. SQL written for idc-index (`FROM index`) is portable here.

## Build & deploy

```bash
docker build -t idc-api .           # bakes the read-only DuckDB file
docker run -p 8080:8080 idc-api     # REST; override CMD with `idc-mcp --http` for MCP
```

The image is stateless (Cloud Run-friendly); rebuild on each IDC release to pick up new
`idc-index-data`.

Full Cloud Run deployment instructions — build/push, `gcloud run deploy`, the DuckDB-memory
sizing rule, IDC-version updates, and the optional hosted MCP service — are in
[`dev/deployment.md`](deployment.md) (with a Cloud Build config at
[`dev/cloudbuild.yaml`](cloudbuild.yaml)).
