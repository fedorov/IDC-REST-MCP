# IDC API v3 — Architecture Overview

This document describes the architecture of the v3 codebase under `src/idc_api/`. For the
*why* (problem statement, decisions, threat model) see [`dev/api_v3_plan.md`](api_v3_plan.md);
for *how to work in it* see [`dev/developer_guide.md`](developer_guide.md).

## Goals that shaped the design

1. **LLM-first.** An MCP server and a REST API are first-class, equal consumers. A capability
   must be exposable as a well-described MCP tool *and* an HTTP endpoint with no duplicated
   logic.
2. **Lean on `idc-index`.** Reuse its bundled Parquet index + DuckDB engine; do not
   reimplement queries, citations, viewer URLs, or downloads from scratch. This deletes v2's
   BigQuery/webapp coupling and SQL-string surgery.
3. **Swappable backend.** Today everything runs locally on Parquet+DuckDB; a BigQuery backend
   must be addable later without touching services or adapters.
4. **Safe by construction.** All data is public and read-only, so the query surface (including
   a raw SQL tool) is sandboxed at the engine level rather than guarded by fragile string
   filtering.

## The shape: one core, two adapters

```
            ┌──────────────────────┐        ┌──────────────────────┐
            │   REST adapter        │        │    MCP adapter        │
            │   src/idc_api/rest    │        │    src/idc_api/mcp    │
            │   FastAPI routes      │        │    FastMCP tools +    │
            │                       │        │    resources          │
            └──────────┬───────────┘        └───────────┬──────────┘
                       │  (thin: validate + delegate)    │
                       └───────────────┬─────────────────┘
                                       ▼
                         ┌──────────────────────────────┐
                         │           core               │
                         │  src/idc_api/core            │
                         │                              │
                         │  services/  ← domain logic   │
                         │  models.py  ← shared contract │
                         │  filters.py ← filter → SQL    │
                         │  schema.py  ← table registry  │
                         │  backend/   ← QueryBackend    │
                         └───────────────┬──────────────┘
                                         ▼
                         ┌──────────────────────────────┐
                         │  DuckDBBackend (read-only)    │
                         │  over idc-index Parquet       │
                         └──────────────────────────────┘
```

**Hard rule:** `core/` imports neither FastAPI nor MCP. Adapters import `core/`. There is no
business logic or SQL in an adapter — a route/tool validates input and calls a service.

## Layer responsibilities

### `core/backend/` — the engine boundary
[`base.py`](../src/idc_api/core/backend/base.py) defines `QueryBackend`, the only thing
services know about storage. Its surface is intentionally tiny:

- `query(sql, params, max_rows, timeout_s)` — run a **trusted, parameterized** statement that
  *we* authored (bound params, never string interpolation).
- `run_user_sql(sql, max_rows, timeout_s)` — run an **untrusted** caller/LLM statement under
  the sandbox (single read-only SELECT/WITH, row cap, timeout).
- `list_tables()`.

[`duckdb_backend.py`](../src/idc_api/core/backend/duckdb_backend.py) is the MVP implementation
(details below). A `BigQueryBackend` (Phase 3) implements the same three methods and nothing
else changes.

### `core/services/` — domain logic
One module per capability area, each a stateless class wrapping a `QueryBackend` and returning
Pydantic models:

| Service | File | Responsibility |
|---|---|---|
| `DiscoveryService` | [discovery.py](../src/idc_api/core/services/discovery.py) | version, stats, collections, analysis results, attributes, attribute values |
| `CohortService` | [cohort.py](../src/idc_api/core/services/cohort.py) | filters → counts + page of series + download payload |
| `QueryService` | [query.py](../src/idc_api/core/services/query.py) | schema discovery + guarded `run_sql` |
| `ManifestService` | [manifest.py](../src/idc_api/core/services/manifest.py) | public AWS/GCS URLs, manifest text, `idc` commands |
| `ViewerService` | [viewer.py](../src/idc_api/core/services/viewer.py) | OHIF/SLIM viewer URLs |
| `CitationsService` | [citations.py](../src/idc_api/core/services/citations.py) | DOI-based citations |
| `LicenseService` | [licenses.py](../src/idc_api/core/services/licenses.py) | license breakdown |
| `DownloadService` | [download.py](../src/idc_api/core/services/download.py) | local-only file transfer via idc-index |

### `core/models.py` — the shared contract
[models.py](../src/idc_api/core/models.py) holds the Pydantic request/response models returned
by both adapters. Because both surfaces serialize the *same* models, REST JSON and MCP tool
output are structurally identical — this is what the parity test enforces.

### `core/schema.py` and `core/filters.py`
- [schema.py](../src/idc_api/core/schema.py) is the single source of truth for which tables
  exist (`BUNDLED_TABLES`), their column metadata (sourced from
  `idc_index_data.INDEX_METADATA`), and the curated `FILTERABLE_ATTRIBUTES` (each tagged
  `term` or `range`).
- [filters.py](../src/idc_api/core/filters.py) compiles a `CohortFilters` model into a
  parameterized `WHERE` clause. Attribute *names* are whitelisted + double-quoted (identifiers
  can't be bound); attribute *values* are always bound parameters.

### `core/context.py` — wiring
[`AppContext`](../src/idc_api/core/context.py) builds the backend once and instantiates every
service. `get_context()` is a process-wide singleton. Both adapters obtain capabilities through
one `AppContext`.

## Request flow

A cohort query, end to end, is the same logic from either surface:

```
REST:  POST /v3/cohort/manifest  ──┐
                                   ├─► AppContext.cohort.build_manifest(filters, page, size)
MCP:   tool build_cohort(terms,…) ─┘        │
                                            ├─ compile_filters() → (where_sql, params)
                                            ├─ backend.query(counts SQL, params)      ← parameterized
                                            ├─ backend.query(rows SQL,  params)        ← parameterized
                                            └─ ManifestService.download_info()         ← URLs + idc cmds
                                            ▼
                                   ManifestResponse (Pydantic)
                                            ▼
REST → JSON response          MCP → structured tool result
```

The guarded SQL path is the only one that takes caller SQL:

```
run_sql(sql) → QueryService.run_sql → backend.run_user_sql(sql)
   _validate_select(sql)            # single read-only SELECT/WITH only
   wrap: SELECT * FROM (<sql>) LIMIT max_rows+1   # engine-level row cap + truncation flag
   _execute(... timeout_s)          # runs in a worker thread; cursor.interrupt() on overrun
```

## The DuckDB engine

[`duckdb_backend.py`](../src/idc_api/core/backend/duckdb_backend.py):

1. **Build once.** `build_database_file()` creates a DuckDB file by `CREATE TABLE … AS SELECT
   * FROM read_parquet(<bundled parquet>)` for each table in `schema.BUNDLED_TABLES` (main
   series table exposed as `index`, matching idc-index/IDC docs). `_ensure_database()` caches
   it in the temp dir keyed by the `idc-index-data` version (atomic publish via `os.replace`),
   or uses a prebuilt file when `IDC_API_DUCKDB_PATH` is set (the Docker image bakes one).
2. **Reopen read-only + hardened.** The serving connection is opened
   `read_only=True` with a `config` dict applying DuckDB's untrusted-SQL hardening
   (`enable_external_access=false`, no extensions, memory/thread/temp caps,
   `lock_configuration=true`). See `_hardening_config()`.
3. **Per-request cursors.** DuckDB connections aren't thread-safe, so every query runs on a
   fresh `self._con.cursor()`. FastAPI runs sync routes in a threadpool, so this matters.
4. **Timeouts.** `_execute()` runs the query in a one-shot worker thread; on overrun it calls
   `cursor.interrupt()` and raises `QueryTimeoutError`.

### Why hardening is applied at connect-time (a real gotcha)
DuckDB shares **one database instance per file path within a process**. If hardening were
applied with post-connect `SET … ; SET lock_configuration=true`, a *second* backend opening the
same file (e.g. REST + a test in one process) would hit the already-locked instance and fail.
Passing identical hardening via the connect-time `config` dict makes repeated opens reuse the
instance cleanly. (In production REST and MCP run in separate processes anyway.)

## SQL safety model (summary)

Two cases, two defenses:

- **Curated queries** (services) — we own the SQL shape and interpolate user *values* →
  **parameterized** (`?` bind params), per OWASP.
- **Raw `run_sql`** — the whole statement is untrusted → **sandboxed read-only DuckDB**:
  public data (nothing to steal), read-only (nothing to modify), external access disabled
  (no host file/network reach), single-SELECT validation, row cap + timeout, all running in a
  read-only container.

Full threat model and primary-source references (OWASP, DuckDB Securing guide) are in
[`dev/api_v3_plan.md`](api_v3_plan.md) → *Safety for the guarded SQL tool*.

## Deployment modes

| | REST API | MCP — local (stdio) | MCP — hosted (http) |
|---|---|---|---|
| Runs | hosted (Cloud Run) | on the user's machine | hosted |
| Filesystem access to caller | no | **yes** | no |
| `download_cohort` / `POST /download` | 501 (disabled) | performs real transfer via idc-index | disabled |
| Data retrieval | manifest + URLs + `idc` command | real download **or** manifest | manifest + URLs |

`main()` in [`mcp/server.py`](../src/idc_api/mcp/server.py) flips
`settings.enable_local_download` on for stdio. `DownloadService.available()` reads that flag at
call time.

The hosted HTTP transport is configured **stateless** (`stateless_http=True`,
`json_response=True` on the `FastMCP(...)` constructor) so each request is self-contained and
the service autoscales across instances like the REST API — no sticky sessions. This is safe
because the server exposes only client-initiated tools + static resources (none of the
server→client features that require a persistent session). The flags affect only the HTTP
transport; stdio is unaffected. See the constructor comment in `mcp/server.py` for the full
rationale.

## Extension points

- **Add a capability** → add a method to a service (or a new service) returning a model, then a
  REST route in [`rest/app.py`](../src/idc_api/rest/app.py) and an MCP tool in
  [`mcp/server.py`](../src/idc_api/mcp/server.py). Add a parity test. (Step-by-step in the
  developer guide.)
- **Add an index table** (Phase 2) → register it in `schema.BUNDLED_TABLES` (and ensure it's
  fetched), expose targeted service methods/tools. Schema discovery picks it up automatically.
- **Add a backend** (Phase 3) → implement `QueryBackend` (3 methods) in
  `core/backend/bigquery_backend.py`, select it in `AppContext`. Services and adapters are
  untouched.

## Technology choices

| Concern | Choice | Note |
|---|---|---|
| Language/runtime | Python 3.11+ (dev on 3.12) | matches idc-index |
| Web framework | FastAPI + uvicorn | free OpenAPI/Swagger + generated SDKs |
| MCP | official `mcp` SDK (FastMCP) | hand-authored tools, not auto-converted routes |
| Query engine | DuckDB over Parquet | ms latency, no GCP/auth, matches idc-index |
| Models/validation | Pydantic v2 | one contract for both adapters |
| Packaging | `uv` + lockfile, hatchling src-layout | reproducible installs |
