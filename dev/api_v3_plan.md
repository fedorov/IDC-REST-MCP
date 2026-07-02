# IDC API — Design & Implementation Plan

## Context

IDC previously ran an older REST API: a Flask app that proxied a separate webapp for
metadata and forwarded generated SQL to **BigQuery**, then rewrote that SQL by hand
(string surgery) to inject counts/grouping. That approach was operationally fragile,
latency-bound, tightly coupled to private webapp internals, and stopped at "manifest
only" (no download). Meanwhile **`idc-index`** answers the same questions in
milliseconds from local Parquet via DuckDB, and is now the de-facto recommended path
for IDC users.

**Goal:** a greenfield service that (a) drops the BigQuery/webapp coupling and
SQL-string surgery in favor of the `idc-index` Parquet+DuckDB engine, and (b) is
designed **LLM-first** — built so a hosted REST API and an **IDC MCP server** are two
thin adapters over one shared core, developed and iterated together. All IDC data is
open; **no authentication** is required of callers.

### Decisions locked with the user
- **Query backend:** `idc-index` Parquet + in-process **DuckDB** for the MVP. Put the
  backend behind an interface so a **BigQuery fallback can be added later** without
  touching the service or adapter layers.
- **SQL access:** ship **curated high-level tools _and_ a guarded read-only SQL tool**
  (+ schema-discovery). LLMs are strong at SQL; data is open so injection is moot — the
  real guard is a read-only connection with external access disabled and row/time caps.
- **Data retrieval:** REST returns **manifests + public URLs + an `idc download` command**;
  the MCP server **in local (stdio) mode** can additionally perform the real download via
  `idc-index`. Hosted/remote MCP behaves like REST (manifests only). No server-side
  staging/zipping (collections reach TBs).
- **MVP scope:** **Core radiology + discovery** and **Viewer URLs + citations + licenses**.
  Specialized indices (ct/mr/pt, seg/ann/rtstruct, sm) and clinical data are later phases.

## Architecture: one core, two adapters

The single most important decision. A backend-agnostic **core** library holds all domain
logic and returns Pydantic models; a **FastAPI** app and an **MCP server** are thin
adapters that call the same core. This is what lets us "iterate on both at the same time":
add a capability once, expose it in both surfaces, test it once.

```
src/idc_api/
  core/
    backend/
      base.py            # QueryBackend interface (run_sql, get_schema, list_tables)
      duckdb_backend.py  # MVP: read-only DuckDB over idc-index Parquet
      bigquery_backend.py# later: same interface, proxies bigquery-public-data
    services/
      discovery.py       # collections, analysis_results, versions, attributes, values
      cohort.py          # structured filters -> SQL -> counts + manifest rows
      query.py           # guarded read-only SQL + schema discovery
      manifest.py        # s5cmd manifest, https URLs (AWS+GCS), `idc download` command
      viewer.py          # OHIF/SLIM viewer URLs
      citations.py       # DOI-based citations (APA/BibTeX/CSL/Turtle)
      licenses.py        # license breakdown for a selection
      download.py        # LOCAL-ONLY: real file transfer via idc-index/s5cmd
    models/              # Pydantic request/response models = the shared contract
    schema/              # index column metadata, filterable-attribute + filter defs
  rest/                  # FastAPI app: routers are ~10-line wrappers over services
  mcp/                   # MCP server: hand-authored tools over the same services
  settings.py
```

**Rule:** `core/` imports neither FastAPI nor MCP. Adapters import `core/`. No business
logic in adapters.

### Reuse `idc-index` instead of reimplementing
`idc-index` is already most of the core. Depend on `idc-index` + `idc-index-data` and
reuse, rather than rebuild:
- Query engine source: `idc_index_data.IDC_INDEX_PARQUET_FILEPATH` /
  `PRIOR_VERSIONS_INDEX_PARQUET_FILEPATH` / `INDEX_METADATA` (schema, URLs, filepaths for
  all ~16 indices).
- `IDCClient` methods to wrap directly: `get_idc_version()`, `indices_overview` /
  `get_index_schema()` (schema discovery), `get_viewer_URL()`, `citations_from_selection()`
  (+ `CITATION_FORMAT_*`), `download_from_selection()` / `download_dicom_series()` (local
  download), `fetch_index()` (later phases).
- **Do not** reuse `IDCClient`'s single shared DuckDB connection for serving — DuckDB
  connections aren't thread-safe. Instead `duckdb_backend.py` opens its own **read-only**
  connection over the same Parquet and hands out per-request cursors (see Safety).

No forked code, no SQL string surgery, no webapp dependency, no shared-secret token.

## MVP capabilities (each = one core service, one REST route, one MCP tool)

**Discovery**
- `get_idc_version` / `get_stats` — current version + headline counts.
- `list_collections` / `get_collection` — from `collections_index` (cancer_types,
  tumor_locations, species, subjects, supporting_data, license, DOI).
- `list_analysis_results` — derived datasets (segmentations, annotations, radiomics).
- `list_attributes` — filterable fields + their table + type.
- `get_attribute_values` — distinct values + counts for a categorical field. **Critical
  for LLMs:** lets an agent ground filter values instead of hallucinating them.

**Cohort / manifest**
- `build_manifest` — structured filters over core `index` columns → distinct counts at
  patient/study/series level, total size, paginated rows, and the manifest payload.
  Simple attribute→values + a few range filters; complex selection goes through SQL.
- `get_patients` / `get_studies` / `get_series` — hierarchical browse (mirror `idc-index`).

**Query** (the LLM power feature)
- `list_tables` / `get_table_schema` — from `indices_overview`.
- `sql_query` — guarded read-only DuckDB SELECT (see Safety).

**Answer-grounding & reproducibility**
- `get_viewer_url` — OHIF (radiology) / SLIM (pathology) link for a study/series.
- `get_citations` — citations for a selection, format-selectable.
- `get_licenses` — license breakdown (CC-BY vs CC-BY-NC vs custom) for a selection.

**Retrieval**
- `get_manifest` — s5cmd manifest + public https URLs (AWS+GCS) + `idc download` command.
- `download_cohort` — **MCP local mode only**; performs the transfer via `idc-index`.
  Absent/disabled on the hosted REST + remote MCP surface.

## LLM-first details (apply throughout)
- **Prescriptive tool descriptions** — every MCP tool says *when to call it*, not just what
  it does (e.g. "Call this before filtering, to get valid values for an attribute"). This
  measurably improves tool selection on current Claude models.
- **Token-efficient by default** — return counts/summaries + sizes, not 65k-row dumps.
  Default `LIMIT`, cursor pagination, and surface byte sizes so an agent can warn before a
  TB-scale download. Keep a hard cap and a small default page size.
- **Discovery-first ordering** — schema tools + `get_attribute_values` exist so an agent
  grounds queries in real columns/values first (the IDC skill stresses this).
- **Cross-table discoverability** — a filter-shaped question can secretly be a join: e.g.
  *segmented anatomy* lives in `seg_index.SegmentedPropertyType_CodeMeanings`, while
  `BodyPartExamined` only describes the source acquisition. Without explicit routing, an agent
  grounds on the main `index`, finds a plausible value, and concludes the real metadata doesn't
  exist (observed failure). Hence: `INSTRUCTIONS`/tool descriptions state the `<modality>_index`
  naming convention and the rule "property not in `list_attributes` → check `list_tables`", and
  attribute responses carry a `note` caveat at the decision point.
- **MCP resources** — expose static reference (index schemas, "how to query IDC" guide) as
  MCP *resources* so agents can read them without burning tool calls.
- **Structured errors** — typed error payloads with `is_error`; never leak stack traces.

## Safety for the guarded SQL tool

### Background: what SQL injection is, and why it changes shape here
"SQL injection" (SQLi) is the classic web vulnerability where an app builds a query by
**concatenating untrusted input into a query string**, so an attacker can smuggle in extra
SQL — e.g. input `x'; DROP TABLE users; --` turning a lookup into a deletion. Its damage
comes from three things: **reading data you shouldn't**, **modifying/deleting data**, and
(via DB features) **reaching the host's files/network**. OWASP's primary defense is to stop
building dynamic queries by string concatenation and use **parameterized queries** instead
([OWASP: SQL Injection](https://owasp.org/www-community/attacks/SQL_Injection),
[OWASP SQL Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html),
[OWASP Query Parameterization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Query_Parameterization_Cheat_Sheet.html)).

Our situation defuses the first two consequences by construction: the data is **fully
public** (nothing secret to steal) and the connection is **read-only** (nothing to
modify/delete). So the residual risk is only the third: an engine feature being abused to
**read the host's local files or reach the network**, or to **exhaust resources** (a giant
or runaway query). Those are real and we guard them explicitly below. Note the threat model
is unusual: with an LLM SQL tool, the *entire query is model-authored*, so we treat **every
query as untrusted** and rely on a sandboxed engine — not on parameterization, which only
helps when *we* own the query structure and interpolate untrusted *values*.

### Two distinct cases, two defenses
1. **Curated tools** (e.g. `build_manifest`, `get_attribute_values`) — we own the query
   shape and interpolate user-supplied filter *values*. Use **parameterized queries**
   (DuckDB prepared statements / bound params), exactly per the OWASP primary defense.
2. **Raw `sql_query` tool** — the caller/LLM supplies the whole statement. Defense is a
   **sandboxed, read-only DuckDB connection**, not input parsing.

### Hardening the raw SQL connection (DuckDB's own recommendations)
DuckDB documents how to run untrusted SQL safely in
[Securing DuckDB](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview)
(see also [Securing Extensions](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/securing_extensions)
and the CLI [Safe Mode](https://duckdb.org/docs/stable/clients/cli/safe_mode)). Apply
their verbatim settings on the serving connection, then lock them:
```sql
SET enable_external_access = false;      -- blocks ATTACH/COPY and read_csv/parquet/json
                                         -- to arbitrary files/URLs (and httpfs exfiltration)
SET autoload_known_extensions = false;   -- no implicit extension loading
SET autoinstall_known_extensions = false;
SET allow_community_extensions = false;  -- no untrusted extension code
SET memory_limit = '4GB';                -- resource caps (tune to the box)
SET threads = 4;
SET max_temp_directory_size = '4GB';
SET lock_configuration = true;           -- prevents re-enabling any of the above
```
Open the connection itself `read_only=True` over the Parquet, accept only `SELECT`/`WITH`
(reject everything else by parsing the statement, not regex), enforce a **statement
timeout** and a **max result-row cap**, and use a **per-request cursor** (DuckDB
connections aren't thread-safe).

### Defense in depth
DuckDB is explicit that these settings "cannot provide complete protection against all
attack vectors, especially when executing untrusted SQL" and recommends combining them
with **OS/container-level sandboxing**. We get that for free: the service runs in a
read-only container (Cloud Run) with no secrets mounted and only the public Parquet on
disk — so even a hypothetical engine escape reaches nothing sensitive. This matches the
general MCP guidance to treat all tool inputs as untrusted and apply defense in depth
([Securing DuckDB](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview)).

### References
- OWASP — SQL Injection (what it is, consequences): https://owasp.org/www-community/attacks/SQL_Injection
- OWASP — SQL Injection Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html
- OWASP — Query Parameterization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Query_Parameterization_Cheat_Sheet.html
- DuckDB — Securing DuckDB: https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview
- DuckDB — Securing Extensions: https://duckdb.org/docs/stable/operations_manual/securing_duckdb/securing_extensions
- DuckDB — CLI Safe Mode: https://duckdb.org/docs/stable/clients/cli/safe_mode

## Tech stack & deployment
- **Python 3.11+**, **FastAPI** + uvicorn/gunicorn, **Pydantic v2**. FastAPI gives OpenAPI
  docs + generated client SDKs for free (serves the non-Python/web-integration audience).
- **MCP:** the official `mcp` Python SDK (FastMCP server API). Hand-author tools over
  `core/` rather than auto-converting REST routes — auto-conversion yields generic,
  poorly-described tools. Same image serves remote MCP (HTTP) next to REST; local MCP runs
  via **stdio** from a `pip install idc-mcp` / `uvx` entrypoint.
- **Deps & build:** `uv` + lockfile for reproducible installs. Deps: `idc-index`,
  `idc-index-data`, `duckdb`, `pyarrow`, `fastapi`, `pydantic`, `mcp`. **Slim Dockerfile** —
  no MySQL/m2crypto/xmlsec/swig build weight. The Parquet index ships inside the image via
  `idc-index-data` (no external DB, no GCP for the MVP).
- **Hosting:** **Cloud Run** (stateless, scale-to-zero) rather than App Engine Flex —
  simpler and cheaper for a stateless service. Pin `idc-index-data`; rebuild the image on
  each IDC release; `get_idc_version` reports what's served.
- **Caching/CDN:** discovery responses change only per release — add `Cache-Control` so
  they're effectively static between versions.

## Phased roadmap
- **Phase 1 (MVP):** core scaffold + `DuckDBBackend` + discovery + `build_manifest` +
  guarded `sql_query` + schema discovery + viewer/citations/licenses + manifest/URLs.
  Both REST and MCP. Cloud Run deploy + local stdio MCP entrypoint.
- **Phase 2:** specialized indices (ct/mr/pt/contrast/volume_geometry, seg/ann/rtstruct,
  sm/sm_instance) as tables + targeted tools; local `download_cohort`; CDN caching
  (see [caching_and_cdn.md](caching_and_cdn.md)); generated client SDK + examples.
- **Phase 3:** `BigQueryBackend` behind the same interface for full DICOM metadata,
  per-segment anatomy, and SR radiomics/qualitative measurements; clinical index + tables.

## Verification
- **Unit/golden tests (offline, deterministic):** run core services against the bundled
  Parquet and assert results match `idc-index` directly (e.g. `build_manifest` counts ==
  an equivalent `IDCClient.sql_query`, citations/viewer URLs match `IDCClient` output).
- **Contract test:** for each capability, assert REST route, MCP tool, and core service
  return the same payload from the same inputs — guarantees the two surfaces stay in sync.
- **SQL guard tests:** confirm non-SELECT, file reads, `httpfs`, and over-cap result sets
  are rejected; confirm timeout fires.
- **REST:** `uvicorn` locally, exercise `/docs` (Swagger) + a few cohort/SQL calls.
- **MCP:** run with the **MCP Inspector**, then wire the local stdio server into Claude
  Code/Desktop and run real prompts ("find breast MRI in IDC, show counts, give me a
  download command") — this is the parallel REST+MCP dev loop the project is built around.
- **Accuracy smoke test:** reproduce a representative manifest query by hand and confirm
  the returned series/counts are correct.
