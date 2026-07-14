# IDC API — User Guide

How to query the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/)
through the IDC API. The same capabilities are available two ways:

- **REST API** (HTTP/JSON) — for scripts, apps, and notebooks.
- **MCP server** — the same capabilities as Model Context Protocol tools, so LLM agents
  (Claude, etc.) can query IDC directly.

All IDC data is **public and open — no authentication, account, or credentials required.**

> Looking for install/run/deploy instructions? See [`README.md`](../README.md). For the
> internal design, see [`dev/architecture.md`](../dev/architecture.md).

---

## 1. Concepts

### The data model

IDC stores public cancer imaging as **DICOM**, organized as a hierarchy:

```
Patient → Study → Series        (the DICOM hierarchy; `index` has one row per Series)
   labelled by two independent grouping axes:
     collection_id              the source dataset (e.g. `nlst`, `tcga_luad`); a patient
                                belongs to exactly one collection
     analysis_result_id         a derived dataset (segmentations/annotations/radiomics);
                                a single analysis result can span *multiple* collections
```

`collection_id` and `analysis_result_id` are **orthogonal** — an analysis result is *not*
nested under one collection, so filtering by a `collection_id` will not necessarily capture all
of an analysis result's series (and vice versa). Filter on whichever axis you actually mean.
(`analysis_results_index` lists each result's source collections in its plural `collections`
field.)

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
| **Retrieval** | "Give me the download links" | `POST /v3/cohort/manifest.txt` | `get_cohort_urls` |
| **SQL** | "Run my custom query" + schema | `GET /v3/tables`, `/v3/tables/{table}`, `POST /v3/sql` | `list_tables`, `get_table_schema`, `run_sql` |
| **Side tools** | View / cite / license-check a cohort | `GET /v3/viewer-url`, `POST /v3/citations`, `POST /v3/licenses` | `get_viewer_url`, `get_citations`, `get_licenses` |

How they relate, in one paragraph: **Discovery** hands you the lay of the land *and the
vocabulary* — the attribute names and valid values you'll filter on. **Cohort** turns a chosen
combination of that vocabulary into distinct counts, a page of matching series, and a
ready-to-use download payload (it reuses the **Retrieval** logic to build that payload).
**Retrieval** is the download half on its own — public `s3://` URLs, a full `manifest.txt`, and
`idc` CLI commands. **SQL** is the bypass: when your selection needs a
`GROUP BY`, a join, or an aggregation that structured cohort filters can't express, you write a
read-only `SELECT` against `index` (and the specialized indices it joins to — see
[What you can query](#what-you-can-query-tables-available-to-sql)). The side tools (viewer /
citations / licenses) all operate on the *same* cohort filters.

> **Cohort or SQL?** Use **Cohort** when your selection is attribute filters over series
> metadata (equality/IN + ranges, on the one `index` table) — it's structured, validated, and
> can't be malformed. Use **SQL** for anything *relational or aggregate* — joins, `GROUP BY`,
> "X that *also has* Y", per-group counts — and for properties only a specialized index holds
> (e.g. the anatomy a segmentation contains, in `seg_index`). Don't force a relational question
> through the cohort path, and don't reach for SQL when a plain filter will do. (Anything you can
> `SELECT series_aws_url FROM index WHERE …` for *is* a manifest, so SQL can also produce
> download URLs directly.)

### Recommended workflow

**Start from the shape of your question — not always from "orient."** There are two entry
points; pick by what you're asking:

**A. Simple attribute filter** (e.g. *"breast MRI from NLST"*):
1. **Ground values** — `list_attributes` (what you can filter on) → `get_attribute_values` (the
   *real* values + correct casing). **Don't guess values.** If the property you need isn't among
   the attributes (e.g. *what anatomy a segmentation contains*), it lives in a specialized
   index — switch to path B.
2. **Build & size** — `cohort/counts` (cheap) to sanity-check size, then `cohort/manifest` for
   the series page + download payload.

**B. Relational or aggregate question** (e.g. *"modalities present per collection"*, *"series
matching a joined condition"*) → **go straight to SQL**:
1. **Ground the schema** — `list_tables` → `get_table_schema('index')` (and any other table you
   need). **Don't guess table/column names.**
2. **Query** — `run_sql('SELECT …')`. Select `series_aws_url` (or `SeriesInstanceUID`) if you
   want a manifest out of it.

**Both paths then:** get the data — prefer the returned `idc` commands / `manifest.txt` (direct
from S3/GCS; see [§4](#4-getting-the-data)) — and **be a good citizen** — check `licenses`
(CC BY vs CC BY-NC) and include `citations` when you publish.

> Orientation (`stats`, `list_collections`, `list_analysis_results`) helps you *discover* a
> dataset, but it can't scope a relational question — skip it and go to SQL when you already
> know what you're joining.

> **Explore narrow, then widen.** While you're still figuring out a query, keep result sizes
> small — a low `max_rows` / `limit` / `page_size`, or a COUNT/GROUP BY instead of fetching raw
> rows — and raise the limit only once you know you need the full set. Every tool caps output by
> default and flags truncation, so a peek stays cheap; large unfiltered results mostly just waste
> the agent's context.
>
> **Knowing you got it all.** Size-capped responses include a `truncated` boolean:
> `truncated: false` means the result is complete; `true` means raise the limit and re-check (or
> narrow/aggregate). `run_sql`'s `max_rows` is clamped to a server ceiling (`SQL_MAX_ROWS_CAP`),
> so there is no "unlimited" value — for bulk *series*, use the cohort/manifest tools rather than
> dumping rows through `run_sql`.

### What you can query (tables available to SQL)

`run_sql` / `list_tables` can reach the **bundled** tables plus the **specialized** indices,
which are fetched from idc-index at build time and joined in SQL (the cohort filters still apply
to `index` only). Specialized indices are named `<modality>_index` after the DICOM Modality of
the series they describe — if a Modality value is central to your question (SEG, CT, SM, …),
check its index. Join them to `index` on `SeriesInstanceUID` (`clinical_index` is the exception:
per-collection, keyed by `collection_id`).

| Table(s) | Granularity / what it adds |
|---|---|
| `index` | one row per **series** — the main table |
| `collections_index` | one row per collection (curated metadata) |
| `analysis_results_index` | one row per analysis result |
| `version_metadata_index` / `prior_versions_index` | IDC release versions / removed series |
| `seg_index`, `ann_index`, `ann_group_index`, `rtstruct_index` | segmentations / annotations / RT structures: **what was segmented** (`SegmentedPropertyType_CodeMeanings` — `BodyPartExamined` reflects the source acquisition, not this) and the **reference** to the image series they derive from (`segmented_SeriesInstanceUID` / `referenced_SeriesInstanceUID`) |
| `ct_index`, `mr_index`, `pt_index` | per-modality acquisition parameters (slice thickness, kVp, TE/TR, injected dose…) |
| `sm_index`, `sm_instance_index` | slide-microscopy (pathology) series / instance metadata |
| `contrast_index`, `volume_geometry_index` | contrast agent / 3D volume geometry |
| `clinical_index` | per-collection clinical-table data dictionary |

This is what makes **relational** questions answerable. For example, *"pathology slides that
have a segmentation of a specific structure"* — impossible against `index` alone — is a join of
`index` (the slides) to `seg_index` (the segmentations) on the segmented image series:

```sql
SELECT i.collection_id, count(DISTINCT i.SeriesInstanceUID) AS slides
FROM index i
JOIN seg_index seg ON seg.segmented_SeriesInstanceUID = i.SeriesInstanceUID
WHERE i.Modality = 'SM'                                    -- slide microscopy (pathology)
  AND list_contains(seg.SegmentedPropertyType_CodeMeanings, 'Nucleus')  -- the segmented structure
GROUP BY 1 ORDER BY slides DESC
```

> **Array columns:** columns whose schema type is `STRING[]` (e.g. the `*_CodeMeanings`
> columns above) hold a *list* of values per row — match elements with
> `list_contains(col, 'value')`, not `=` or `LIKE`. If a query is invalid, the error response
> carries DuckDB's own message (including its "Did you mean …?" suggestions), so fix and retry.

> **Still BigQuery-only:** a handful of things remain outside these indices — *per-individual-segment*
> detail (each segment rather than the series-level `DISTINCT`-aggregated code lists in
> `seg_index`), DICOM SR quantitative/qualitative measurements (radiomics), and private DICOM
> elements. For those, use [`idc-index`](https://github.com/ImagingDataCommons/idc-index) with
> BigQuery. Note: `seg_index`'s multi-valued code columns are aggregated independently, so
> positional correspondence between them is not preserved.

### Clinical (non-imaging) data

Many collections ship **clinical** data — demographics, diagnoses, cancer staging, therapies,
labs, outcomes — alongside the images. It comes in two layers:

- **`clinical_index`** — a *data dictionary*: one row per (collection, table, column) with a
  human-readable `column_label` and an array of coded `values` (`option_code` →
  `option_description`). Use it to discover *what* clinical attributes a collection has and what
  their codes mean. It's a normal table — query it with `run_sql` (it joins to `index` on
  `collection_id`).
- **Per-collection clinical tables** (e.g. `nlst_canc`) — the actual clinical rows. These are
  registered under a separate **`clinical` schema** and queried as `clinical.<table>`. They are
  kept out of `list_tables` (there are ~150 of them) and discovered with dedicated capabilities
  instead. Each joins to imaging on **`dicom_patient_id = index.PatientID`** (not
  `SeriesInstanceUID`). Clinical data is *not harmonized* across collections — table and column
  names vary, so always discover before querying.

| Capability | REST | MCP |
|---|---|---|
| List clinical tables (optionally for one collection) | `GET /v3/clinical/tables[?collection_id=…]` | `list_clinical_tables` |
| Columns + human-readable labels of a clinical table | `GET /v3/clinical/tables/{table}` | `get_clinical_table_schema` |
| Read a clinical table's rows (capped) | `GET /v3/clinical/tables/{table}/rows` | `get_clinical_table` |

For relational questions — filtering by a clinical attribute, or joining clinical data to
imaging — use `run_sql` against `clinical.<table>`. For example, *"NLST patients imaged with CT
whose cancer is stage IV (code `400`)"*:

```sql
SELECT count(DISTINCT i.PatientID) AS patients
FROM index i
JOIN clinical.nlst_canc c ON c.dicom_patient_id = i.PatientID
WHERE i.collection_id = 'nlst' AND i.Modality = 'CT'
  AND c.clinical_stag = '400'
```

> Clinical tables exist only when `clinical_index` is included in the build (the default
> `IDC_API_INCLUDE_INDICES=all` includes it; the clinical tools return a clear "not included"
> error otherwise).

---

## 2. Using the REST API

Start the server (see [`README.md`](../README.md) for install):

```bash
uv run idc-api          # http://127.0.0.1:8000  — Swagger UI at /v3/docs
```

### Endpoint reference

| Method & path | Purpose |
|---|---|
| `GET /v3/version` | IDC data release served (e.g. `v24`) + pinned index version, **and** this server's own software version (`api_version`, plus `build` if the deploy stamped one) |
| `GET /v3/stats` | Headline totals (collections, patients, studies, series, size_TB) |
| `GET /v3/collections` | List collections (datasets) |
| `GET /v3/collections/{id}` | Collection detail: counts, modalities, license breakdown |
| `GET /v3/analysis_results` | Derived datasets (segmentations/annotations) |
| `GET /v3/attributes` | Filterable attributes (name, type, term/range, categorical) |
| `GET /v3/attributes/{attr}/values?limit=` | Distinct values + counts for an attribute, plus a `note` caveat when one applies (e.g. `BodyPartExamined` ≠ segmented anatomy) |
| `GET /v3/tables` | Tables available to SQL |
| `GET /v3/tables/{table}` | Column schema for a table |
| `GET /v3/clinical/tables?collection_id=` | Per-collection clinical tables (optionally one collection) |
| `GET /v3/clinical/tables/{table}` | Clinical table columns + human-readable labels |
| `GET /v3/clinical/tables/{table}/rows?max_rows=` | Clinical table rows (capped) |
| `POST /v3/cohort/counts` | Distinct counts for a filter (cheap) |
| `POST /v3/cohort/manifest` | Counts + a page of series + download payload |
| `POST /v3/cohort/manifest.txt` | Full manifest as `text/plain` (`s3://`; `source=gcs` reaches GCS's S3-compatible endpoint) |
| `POST /v3/sql` | Guarded read-only SQL (DuckDB) |
| `GET /v3/viewer-url` | OHIF/SLIM viewer link for a study/series |
| `POST /v3/citations` | Citations for a cohort |
| `POST /v3/licenses` | License breakdown for a cohort |

### Worked examples

Every endpoint below is also documented interactively at `/v3/docs` (Swagger UI), with a filled-in
request/response example for each.

**Discover valid values before filtering:**

```bash
curl -s 'localhost:8000/v3/attributes/Modality/values?limit=10'
```

**Other read-only lookups (GET)** — no body, just the URL:

```bash
curl -s localhost:8000/v3/version                 # data release + this server's build
curl -s localhost:8000/v3/stats                   # headline totals
curl -s localhost:8000/v3/collections             # list datasets
curl -s localhost:8000/v3/collections/nlst        # one collection's detail
curl -s localhost:8000/v3/analysis_results        # derived datasets
curl -s localhost:8000/v3/attributes              # filterable attributes
curl -s localhost:8000/v3/tables                  # tables available to SQL
curl -s localhost:8000/v3/tables/index            # one table's column schema
curl -s 'localhost:8000/v3/clinical/tables?collection_id=nlst'          # clinical tables for a collection
curl -s localhost:8000/v3/clinical/tables/nlst_canc                     # clinical table columns + labels
curl -s 'localhost:8000/v3/clinical/tables/nlst_canc/rows?max_rows=100' # clinical rows (capped)
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

**Citations for a cohort** — body wraps the filter, like `manifest`:

```bash
curl -s localhost:8000/v3/citations \
  -H 'content-type: application/json' \
  -d '{"filters": {"terms": {"collection_id": ["nlst"]}}, "citation_format": "apa"}'
```

**Viewer link** for a study (or pass `series_instance_uid=`):

```bash
curl -s 'localhost:8000/v3/viewer-url?study_instance_uid=1.3.6.1.4.1.14519.5.2.1.7009.9004.983700485806071099502442051273'
```

---

## 3. Using the MCP server (LLM agents)

```bash
uv run idc-mcp                                       # stdio (local)
uv run idc-mcp --http --host 0.0.0.0 --port 8080     # hosted/shared
```

### Tools, by surface

- **Discovery:** `get_idc_version`, `get_stats`, `list_collections`, `get_collection`,
  `list_analysis_results`, `list_attributes`, `get_attribute_values`
- **Schema (for SQL):** `list_tables`, `get_table_schema`
- **Clinical data:** `list_clinical_tables`, `get_clinical_table_schema`, `get_clinical_table`
- **Cohort / query:** `build_cohort`, `run_sql`
- **Retrieval & side tools:** `get_cohort_urls`, `get_viewer_url`,
  `get_citations`, `get_licenses`
- **Resources:** `idc://guide` (data model + recommended workflow), `idc://tables`,
  `idc://schema/{table}`

Tool descriptions are prescriptive about *when* to call each one, and the server ships an
`idc://guide` resource with the same conceptual model as this document — so a capable agent can
follow the recommended workflow without extra prompting.

> **Relation to the [IDC Claude Skill](https://github.com/ImagingDataCommons/imaging-data-commons-skill).**
> The skill is a *different access path* to the same data: it has the agent write and run Python
> directly against `idc-index` inside a code-execution sandbox (Claude Code, or Claude
> Desktop/claude.ai with code execution enabled). No server involved, and no network round trip
> for metadata — but it only works where the client can execute Python locally. This MCP server
> (and the REST API) instead expose the same `idc-index`/DuckDB index as callable tools/endpoints
> over the network, for clients that can't or don't want to run code: remote-MCP connectors,
> non-Python agent frameworks, or a curated tool surface instead of hand-written SQL. Both share
> the same data model and the same "ground first" workflow — pick the skill when Python execution
> is available, pick MCP/REST for network-only clients or the hosted, zero-setup path.

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

### Connecting to the hosted MCP server

The production service is public and unauthenticated at
**`https://api.imaging.datacommons.cancer.gov/mcp`** — point any remote-MCP client (a custom/
remote connector in Claude, or another spec-conformant client) at that URL directly; no API key
or config file needed. If you deploy your own instance over HTTP (the `--http` form above),
point the client at `https://<service-url>/mcp` (note the `/mcp` path) instead. The HTTP transport
is **streamable-HTTP,
configured stateless with plain-JSON responses** — each request is self-contained, so:

- **Any spec-conformant remote-MCP client works**, and the service autoscales behind a plain
  load balancer with no session affinity or sticky routing.
- **No session handshake is needed to script it** — you can `POST` a `tools/list` or
  `tools/call` directly (set `Accept: application/json, text/event-stream`); you don't have to
  `initialize` first or carry an `Mcp-Session-Id` header.
- **Session-bound MCP features are not available** (server→client sampling, elicitation,
  resource subscriptions, streamed progress) — this server exposes only client-initiated tools
  + static resources, so it doesn't use them.

Operator-side detail (deploy command, host-header / DNS-rebinding settings, the autoscaling
rationale) is in [deployment.md](../dev/deployment.md).

---

## 4. Getting the data

**Get a manifest, then pull the files directly from S3/GCS.** All series URLs point at
**public AWS S3 and GCS buckets — no credentials needed** — so the transfer never goes through
the API server (the server never moves bytes; retrieval always means URLs/manifests), works
the same against the hosted or a local instance, and scales to whole collections. Two ways to
do it:

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
   You can also drive the raw URLs yourself with `s5cmd --no-sign-request` (anonymous access).
   Manifest/URL requests take a `source` of `aws` (default) or `gcs` — both give you `s3://`
   URLs, since GCS is reached through its S3-compatible endpoint rather than a `gs://` URL
   (this matches `idc-index`, and is why `idc download-from-manifest` only recognizes `s3://`
   lines). For `source=gcs`, add `--endpoint-url https://storage.googleapis.com` to `s5cmd`.

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
- **`citations`** returns the publications to cite in `apa`, `bibtex`, `csl-json`, or `turtle`:
  the per-dataset citations (from the cohort's source DOIs) in `citations`, and the IDC paper in
  `idc_acknowledgment`. When you publish results using IDC data, include the per-dataset
  citations **and** acknowledge IDC itself by citing the IDC paper
  ([10.1148/rg.230180](https://doi.org/10.1148/rg.230180)); the `recommendation` field restates
  this.

---

## 7. Configuration

Environment variables (prefix `IDC_API_`):

| Variable | Default | Purpose |
|---|---|---|
| `DUCKDB_PATH` | (built on first run) | Path to the read-only DuckDB file |
| `INCLUDE_INDICES` | `all` | Specialized indices to build in: `all`, `none` (bundled only, fully offline), or a comma list (e.g. `seg_index,ct_index`). Ignored when `DUCKDB_PATH` is set. |
| `SQL_MAX_ROWS` | `5000` | Default rows returned by `run_sql` when the caller omits `max_rows` |
| `SQL_MAX_ROWS_CAP` | `10000` | Hard ceiling: a caller-supplied `max_rows` is clamped to this, so no query can dump an unbounded result. The `truncated` flag still signals a capped result |
| `SQL_TIMEOUT_SECONDS` | `30` | Per-query timeout for `run_sql` |
| `DEFAULT_PAGE_SIZE` | `100` | Default `cohort/manifest` page size |
| `MAX_PAGE_SIZE` | `5000` | Upper bound on page size |
| `MANIFEST_HARD_CAP` | `100000` | Max series enumerated into a manifest |
| `CORS_ALLOW_ORIGINS` | `["*"]` | Allowed CORS origins (REST). List value — set as JSON, e.g. `["https://app.example.com"]` |
| `HSTS_MAX_AGE` | `31536000` | `Strict-Transport-Security` max-age (seconds) added to every REST and hosted-MCP response. Default is the production value (1 year); dev/test deploys use `3600` so a bad deploy can't lock browsers out for a year. `0` disables the header |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | REST bind address |
| `SQL_LOG_MODE` | `snippet` | How `run_sql`/`POST /v3/sql` queries appear in the structured audit log: `snippet` (readable, capped) or `hash` (a short digest, no query text at all) |
| `SQL_LOG_CHARS` | `200` | Snippet length when `SQL_LOG_MODE=snippet` |
| `BUILD` | (unset) | Deploy-time build stamp (e.g. a short git SHA). Appended to the software version reported by `GET /v3/version` (`build`), `GET /` and OpenAPI `info.version` (`api_version+build`), and the MCP `serverInfo.version` — so you can confirm which build a hosted instance is running |

---

## See also

- [`README.md`](../README.md) — install, run, and deploy.
- [`dev/architecture.md`](../dev/architecture.md) — internal design (core + two adapters).
- [`dev/deployment.md`](../dev/deployment.md) — Cloud Run deployment.
- [`dev/api_v3_plan.md`](../dev/api_v3_plan.md) — design rationale + SQL threat model.
- [IDC Claude Skill](https://github.com/ImagingDataCommons/imaging-data-commons-skill) — the
  code-execution access path to the same data; see the callout in
  [§3](#3-using-the-mcp-server-llm-agents) for how it relates to this MCP server.
