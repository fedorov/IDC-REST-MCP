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

**Both paths then:** get the data (the returned `idc` commands / `manifest.txt`, or `download`
locally), and **be a good citizen** — check `licenses` (CC BY vs CC BY-NC) and include
`citations` when you publish.

> Orientation (`stats`, `list_collections`, `list_analysis_results`) helps you *discover* a
> dataset, but it can't scope a relational question — skip it and go to SQL when you already
> know what you're joining.

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
| `GET /v3/attributes/{attr}/values?limit=` | Distinct values + counts for an attribute, plus a `note` caveat when one applies (e.g. `BodyPartExamined` ≠ segmented anatomy) |
| `GET /v3/tables` | Tables available to SQL |
| `GET /v3/tables/{table}` | Column schema for a table |
| `GET /v3/clinical/tables?collection_id=` | Per-collection clinical tables (optionally one collection) |
| `GET /v3/clinical/tables/{table}` | Clinical table columns + human-readable labels |
| `GET /v3/clinical/tables/{table}/rows?max_rows=` | Clinical table rows (capped) |
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
- **Clinical data:** `list_clinical_tables`, `get_clinical_table_schema`, `get_clinical_table`
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

### Connecting to the hosted MCP server

If the server is deployed over HTTP (the `--http` form above), point a remote-MCP client at
`https://<service-url>/mcp` (note the `/mcp` path). The HTTP transport is **streamable-HTTP,
configured stateless with plain-JSON responses** — each request is self-contained, so:

- **Any spec-conformant remote-MCP client works**, and the service autoscales behind a plain
  load balancer with no session affinity or sticky routing.
- **No session handshake is needed to script it** — you can `POST` a `tools/list` or
  `tools/call` directly (set `Accept: application/json, text/event-stream`); you don't have to
  `initialize` first or carry an `Mcp-Session-Id` header.
- **Session-bound MCP features are not available** (server→client sampling, elicitation,
  resource subscriptions, streamed progress) — this server exposes only client-initiated tools
  + static resources, so it doesn't use them.
- **`download_cohort` can't write to your machine** over HTTP — retrieval returns a manifest +
  URLs instead (see *Local vs hosted* below). Use the stdio config above when you want real
  downloads.

Operator-side detail (deploy command, host-header / DNS-rebinding settings, the autoscaling
rationale) is in [deployment.md](../dev/deployment.md).

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
