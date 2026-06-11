# IDC API v3 — User Guide

How to query the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/)
through IDC API v3. The same capabilities are available two ways:

- **REST API** (HTTP/JSON) — for scripts, apps, and notebooks.
- **MCP server** — the same capabilities as Model Context Protocol tools, so LLM agents
  (Claude, etc.) can query IDC directly.

All IDC data is **public and open — no authentication, account, or credentials required.**

> Looking for install/run/deploy instructions? See [`README_v3.md`](../README_v3.md). For the
> internal design, see [`dev/architecture.md`](../dev/architecture.md).

---

## 1. Concepts

### The data model

IDC stores public cancer imaging as **DICOM**, organized as a hierarchy:

```
Patient → Study → Series        (the DICOM hierarchy; `index` has one row per Series)
   grouped by:
     collection_id              a dataset, e.g. `nlst`, `tcga_luad`
     analysis_result_id         derived annotations/segmentations layered on a collection
```

The main queryable table is **`index`** — one row per **series**, the unit you filter, count,
and download. IDC is large (~100+ TB total), so always check counts/size before downloading.

### The query surfaces, and how they relate

v3 exposes a few distinct "surfaces." They build on each other — this is the part worth
understanding, because picking the right one makes everything else easy:

```
 DISCOVERY ───▶ COHORT ───▶ RETRIEVAL            ⟍
 what exists?   how big is    download links       ⟍   SQL  (escape hatch:
 what can I     my filtered   (URLs, manifest,      ⟋   anything the structured
 filter on?     selection?    idc commands)        ⟋    surfaces can't express)
       │            ▲
       └─ provides the vocabulary (attributes + valid values) ─┘
```

| Surface | Answers | REST | MCP tools |
|---|---|---|---|
| **Discovery** | "What exists? What can I filter on?" | `GET /v3/version`, `/v3/stats`, `/v3/collections`, `/v3/collections/{id}`, `/v3/analysis_results`, `/v3/attributes`, `/v3/attributes/{attr}/values` | `get_idc_version`, `get_stats`, `list_collections`, `get_collection`, `list_analysis_results`, `list_attributes`, `get_attribute_values` |
| **Cohort** | "How big is *my* selection, and what's in it?" | `POST /v3/cohort/counts`, `POST /v3/cohort/manifest` | `build_cohort` |
| **Retrieval** | "Give me the download links / files" | `POST /v3/cohort/manifest.txt`, `POST /v3/download` | `get_cohort_urls`, `download_cohort` |
| **SQL** | "Run my custom query" + schema | `GET /v3/tables`, `/v3/tables/{table}`, `POST /v3/sql` | `list_tables`, `get_table_schema`, `run_sql` |
| **Side tools** | View / cite / license-check a cohort | `GET /v3/viewer-url`, `POST /v3/citations`, `POST /v3/licenses` | `get_viewer_url`, `get_citations`, `get_licenses` |

How they relate, in one paragraph: **Discovery** hands you the lay of the land *and the
vocabulary* — the attribute names and valid values you'll filter on. **Cohort** turns a chosen
combination of that vocabulary into distinct counts, a page of matching series, and a
ready-to-use download payload (it reuses the **Retrieval** logic to build that payload).
**Retrieval** is the download half on its own — public `s3://`/`gs://` URLs, a full
`manifest.txt`, and `idc` CLI commands. **SQL** is the bypass: when your selection needs a
`GROUP BY`, a join, or an aggregation that structured cohort filters can't express, you write a
read-only `SELECT` against `index`. The side tools (viewer / citations / licenses) all operate
on the *same* cohort filters.

> A note on cohort vs. SQL: prefer **Cohort** for the common case — it's structured,
> validated, and can't be malformed. Reach for **SQL** only when you need something it can't
> express. Anything you can `SELECT series_aws_url FROM index WHERE …` for *is* a manifest, so
> SQL can also produce download URLs directly.

### Recommended workflow

1. **Orient** — `stats` for the headline totals; `list_collections` / `list_analysis_results`
   to find a dataset.
2. **Ground your filters** — `list_attributes` (what you can filter on) → `get_attribute_values`
   (the *real* values + correct casing). **Don't guess values.**
3. **Build & size a cohort** — `cohort/counts` (cheap) to sanity-check size, then
   `cohort/manifest` for the series page + download payload. Or drop to `sql` for complex logic.
4. **Get the data** — use the returned `idc` commands / manifest, or `download` locally.
5. **Be a good citizen** — check `licenses` (CC BY vs CC BY-NC) and include `citations` output
   when you publish.

---

## 2. Using the REST API

Start the server (see [`README_v3.md`](../README_v3.md) for install):

```bash
uv run idc-api          # http://127.0.0.1:8000  — Swagger UI at /docs
```

### Endpoint reference

| Method & path | Purpose |
|---|---|
| `GET /v3/version` | IDC data release served (e.g. `v24`) + pinned index version |
| `GET /v3/stats` | Headline totals (collections, patients, studies, series, size_TB) |
| `GET /v3/collections` | List collections (datasets) |
| `GET /v3/collections/{id}` | Collection detail: counts, modalities, license breakdown |
| `GET /v3/analysis_results` | Derived datasets (segmentations/annotations) |
| `GET /v3/attributes` | Filterable attributes (name, type, term/range, categorical) |
| `GET /v3/attributes/{attr}/values?limit=` | Distinct values + counts for an attribute |
| `GET /v3/tables` | Tables available to SQL |
| `GET /v3/tables/{table}` | Column schema for a table |
| `POST /v3/cohort/counts` | Distinct counts for a filter (cheap) |
| `POST /v3/cohort/manifest` | Counts + a page of series + download payload |
| `POST /v3/cohort/manifest.txt` | Full manifest as `text/plain` (`s3://` or `gs://`) |
| `POST /v3/sql` | Guarded read-only SQL (DuckDB) |
| `GET /v3/viewer-url` | OHIF/SLIM viewer link for a study/series |
| `POST /v3/citations` | Citations for a cohort |
| `POST /v3/licenses` | License breakdown for a cohort |
| `POST /v3/download` | Local download (returns 501 unless enabled) |

### Worked examples

**Discover valid values before filtering:**

```bash
curl -s 'localhost:8000/v3/attributes/Modality/values?limit=10'
```

**Cheap size check** — the `counts` body is the filter object directly:

```bash
curl -s localhost:8000/v3/cohort/counts \
  -H 'content-type: application/json' \
  -d '{"terms": {"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}}'
```

**Build a cohort** — `manifest` wraps the filter in a request with paging:

```bash
curl -s localhost:8000/v3/cohort/manifest \
  -H 'content-type: application/json' \
  -d '{"filters": {"terms": {"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}},
       "page": 0, "page_size": 3}'
```

Filters: `terms` is `{attribute: [values]}` (equality/IN — OR within an attribute, AND across
attributes); `ranges` is `{attribute: {"gte": x, "lte": y}}`.

**Get the full manifest as plain text** (for `idc download-from-manifest` / `s5cmd`):

```bash
curl -s localhost:8000/v3/cohort/manifest.txt \
  -H 'content-type: application/json' \
  -d '{"filters": {"terms": {"collection_id": ["nlst"]}}, "source": "gcs"}'
```

**Custom query via SQL** (anything the structured filters can't express):

```bash
curl -s localhost:8000/v3/sql \
  -H 'content-type: application/json' \
  -d '{"sql": "SELECT Modality, count(*) n FROM index GROUP BY 1 ORDER BY n DESC", "max_rows": 20}'
```

**License check** — like `counts`, the body is the filter object directly:

```bash
curl -s localhost:8000/v3/licenses \
  -H 'content-type: application/json' \
  -d '{"terms": {"collection_id": ["nlst"]}}'
```

---

## 3. Using the MCP server (LLM agents)

```bash
uv run idc-mcp                                       # stdio (local) — can also download files
uv run idc-mcp --http --host 0.0.0.0 --port 8080     # hosted/shared (manifests only)
```

### Tools, by surface

- **Discovery:** `get_idc_version`, `get_stats`, `list_collections`, `get_collection`,
  `list_analysis_results`, `list_attributes`, `get_attribute_values`
- **Schema (for SQL):** `list_tables`, `get_table_schema`
- **Cohort / query:** `build_cohort`, `run_sql`
- **Retrieval & side tools:** `get_cohort_urls`, `download_cohort`, `get_viewer_url`,
  `get_citations`, `get_licenses`
- **Resources:** `idc://guide` (data model + recommended workflow), `idc://tables`,
  `idc://schema/{table}`

Tool descriptions are prescriptive about *when* to call each one, and the server ships an
`idc://guide` resource with the same conceptual model as this document — so a capable agent can
follow the recommended workflow without extra prompting.

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
command instead). Same tools, two behaviors.

---

## 4. Getting the data

All series URLs point at **public AWS S3 and GCS buckets — no credentials needed.** There are
three ways to retrieve, in rough order of convenience:

1. **Whole collection** — the simplest path; `cohort/manifest`'s download payload emits it for
   you when your filter is a single `collection_id`:
   ```bash
   idc download nlst --download-dir ./idc-data
   ```
2. **A manifest** — save the `manifest.txt` output (or any SQL result's `series_aws_url`
   column) and feed it to the CLI:
   ```bash
   idc download-from-manifest idc_manifest.txt --download-dir ./idc-data
   ```
   You can also use the raw `s3://`/`gs://` URLs with `s5cmd` / `gsutil` (anonymous access).
3. **Local download mode** — `POST /v3/download` (REST) or `download_cohort` (MCP) transfers
   files directly, but **only when the server runs on your machine**. Hosted deployments
   disable it and return a manifest instead. Start with `dry_run` to report size, confirm, then
   run for real.

Install the CLI with `pip install idc-index` (provides the `idc` command).

---

## 5. Guarded SQL — why it's safe

`run_sql` / `POST /v3/sql` accept arbitrary SQL, but the data is **public** (nothing secret)
and the DuckDB connection is opened **read-only** (nothing to modify), so the classic SQL
injection consequences don't apply. The connection is further hardened per DuckDB's
[Securing DuckDB](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview)
guide (external file/network access disabled, no extensions, memory/row/time caps,
configuration locked), and only single read-only `SELECT`/`WITH` statements are accepted.
Values interpolated into curated (non-SQL) queries are always passed as bound parameters
([OWASP](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)).
See [`dev/api_v3_plan.md`](../dev/api_v3_plan.md) for the full threat model.

---

## 6. Licenses & citations

IDC data is open, but licenses vary per series — typically **CC BY** (commercial use allowed)
vs **CC BY-NC** (non-commercial only). Before reusing or redistributing a cohort:

- **`licenses`** returns the series count + size per license for your filter, so you can see at
  a glance whether the selection is commercial-friendly.
- **`citations`** returns the publications to cite (the cohort's source DOIs plus the main IDC
  paper) in `apa`, `bibtex`, `csl-json`, or `turtle`. Include these when you publish.

---

## 7. Configuration

Environment variables (prefix `IDC_API_`):

| Variable | Default | Purpose |
|---|---|---|
| `DUCKDB_PATH` | (built on first run) | Path to the read-only DuckDB file |
| `SQL_MAX_ROWS` | `5000` | Max rows returned by `run_sql` |
| `SQL_TIMEOUT_SECONDS` | `30` | Per-query timeout for `run_sql` |
| `DEFAULT_PAGE_SIZE` | `100` | Default `cohort/manifest` page size |
| `MAX_PAGE_SIZE` | `5000` | Upper bound on page size |
| `MANIFEST_HARD_CAP` | `100000` | Max series enumerated into a manifest |
| `ENABLE_LOCAL_DOWNLOAD` | `false` | Allow `download` to write files locally |
| `CORS_ALLOW_ORIGINS` | — | Allowed CORS origins (REST) |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | REST bind address |

---

## See also

- [`README_v3.md`](../README_v3.md) — install, run, and deploy.
- [`dev/architecture.md`](../dev/architecture.md) — internal design (core + two adapters).
- [`dev/deployment.md`](../dev/deployment.md) — Cloud Run deployment.
- [`dev/api_v3_plan.md`](../dev/api_v3_plan.md) — design rationale + SQL threat model.
