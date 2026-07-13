# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with one house rule: the
**MAJOR version is pinned to the served URL prefix** — `/v3` ↔ `3.y.z`. A breaking change to the
REST or MCP contract means a new prefix (`/v4`) and a new major (`4.0.0`), never a silent break
under `/v3`. See [CONTRIBUTING.md](CONTRIBUTING.md#versioning) for the full policy.

Entries describe **user-visible** change — endpoints, MCP tools, response shapes, configuration.
Refactors, CI, and formatting land in the git history, not here.

## [Unreleased]


## [3.0.0b1] — 2026-07-13

First public release of the v3 API: a rewrite that replaces the v1/v2 service with a single
backend-agnostic core behind two thin adapters (REST + MCP), served from the `idc-index` Parquet
index queried locally with DuckDB.

**This is a beta.** The `/v3` contract may still change in response to feedback before `3.0.0`.
Pin to an exact version if you need stability. Legacy v1/v2 endpoints are unaffected — they are
served by a different backend and v3 lives only under `/v3/*`.

### Added

- **REST API**, entirely under the `/v3` prefix: discovery (`/v3/version`, `/v3/stats`,
  `/v3/collections`, `/v3/analysis_results`, `/v3/attributes`, `/v3/tables`), cohort building,
  retrieval (manifests / cohort URLs, viewer URLs), citations and licenses, guarded SQL, and
  `/v3/health`. Interactive docs at `/v3/docs`; the bare domain redirects there.
- **MCP server** over stdio (local) and streamable-http (hosted at `/mcp`), exposing the same
  capabilities as tools — `list_collections`, `get_collection`, `list_attributes`,
  `get_attribute_values`, `build_cohort`, `get_cohort_urls`, `download_cohort`, `get_viewer_url`,
  `run_sql`, `get_citations`, `get_licenses`, and the clinical/table introspection tools — plus
  an `idc://guide` resource describing the data model and workflow.
- **Guarded SQL** (`POST /v3/sql`, `run_sql`): read-only DuckDB with external access and
  extension loading disabled, single-statement enforcement, a server row cap, and a timeout.
  See [SECURITY.md](SECURITY.md).
- **Specialized indices** joinable to `index` on `SeriesInstanceUID` (`seg_index`, `ann_index`,
  `ct_index`, `mr_index`, `pt_index`, `sm_index`, …) and per-collection **clinical tables** under
  a `clinical` schema, both fetched from `idc-index` releases at build time.
- **Software version reporting**, distinct from the IDC *data* version: `/v3/version` returns
  `api_version` (and `build`, when a deploy stamps `IDC_API_BUILD`); the same string appears in
  the OpenAPI `info.version` and the MCP `initialize` handshake (`serverInfo.version`).
- **Structured audit logging** — one JSON line per REST request and MCP tool call.
  `IDC_API_SQL_LOG_MODE` selects how the guarded SQL query is rendered (`snippet` or `hash`).
- **HSTS**: every REST and hosted-MCP response carries a `Strict-Transport-Security` header
  (NCI security policy). Max-age is configurable via `IDC_API_HSTS_MAX_AGE` — default one year;
  dev/test deploys use 3600.

[Unreleased]: https://github.com/ImagingDataCommons/IDC-REST-MCP/compare/v3.0.0b1...HEAD
[3.0.0b1]: https://github.com/ImagingDataCommons/IDC-REST-MCP/releases/tag/v3.0.0b1
