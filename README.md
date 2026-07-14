# IDC REST API & MCP Server

[![CI](https://github.com/ImagingDataCommons/IDC-REST-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/ImagingDataCommons/IDC-REST-MCP/actions/workflows/ci.yml)

LLM-first **REST API** and **MCP server** for the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/),
backed by the [`idc-index`](https://github.com/ImagingDataCommons/idc-index) Parquet index
queried locally with DuckDB. All IDC data is open — **no authentication required**.

One backend-agnostic **core** library, two thin adapters over it:

- **REST API** (FastAPI) — language-agnostic HTTP access with auto-generated OpenAPI docs.
- **MCP server** — the same capabilities as Model Context Protocol tools, so LLM agents
  (Claude, etc.) can query IDC directly.

Both surfaces share one core, so a capability is implemented and tested once and exposed in
both. For a code-execution alternative to the same `idc-index` data — an agent writes and runs
Python directly, no server involved — see the
[IDC Claude Skill](https://github.com/ImagingDataCommons/imaging-data-commons-skill); the
[User Guide](docs/user-guide.md#3-using-the-mcp-server-llm-agents) explains how the two relate.

> **Status:** **live in production** at `api.imaging.datacommons.cancer.gov` — everything
> listed below, over both REST and MCP. Still to come: CDN caching (see
> [`dev/caching_and_cdn.md`](dev/caching_and_cdn.md)). Per-segment detail, SR radiomics
> measurements, and private DICOM elements are out of scope for this service — see the
> [User Guide](docs/user-guide.md#1-concepts) for where to get them.

## What you can do

Both surfaces expose the same capabilities (full reference in the
[User Guide](docs/user-guide.md)):

- **Discover** what's in IDC — collections, derived analysis results (segmentations,
  annotations), filterable attributes and their valid values, headline stats.
- **Build cohorts** — turn attribute filters into distinct patient/study/series counts, a page
  of matching series, and a ready-to-use download payload.
- **Retrieve** — public `s3://` URLs, a full `manifest.txt`, and `idc` CLI commands; files
  transfer directly from public S3/GCS buckets, never through the server.
- **Run SQL** — guarded read-only DuckDB queries against `index`, the specialized per-modality
  indices (seg/ann/rtstruct, ct/mr/pt, slide microscopy, contrast/geometry), and per-collection
  clinical tables — joins, aggregations, anything the structured filters can't express.
- **Explore clinical data** — discover and read the per-collection clinical tables
  (demographics, staging, therapies, outcomes) and join them to imaging.
- **Publish responsibly** — viewer URLs for visual inspection, per-cohort license breakdowns
  (CC BY vs CC BY-NC), and ready-to-use citations.

**REST or MCP?** Same capabilities, different callers. Use **REST** when *you* write the code —
scripts, apps, notebooks (plain HTTP/JSON, Swagger UI at `/v3/docs`). Use **MCP** when an *LLM
agent* does the querying — the same capabilities as tools, with prescriptive descriptions and an
`idc://guide` resource so agents follow the ground-first workflow on their own. See the User
Guide's [query surfaces](docs/user-guide.md#the-query-surfaces-and-how-they-relate) for how the
capabilities build on each other.

## Use the live service

No install needed — both surfaces are public and unauthenticated at
`https://api.imaging.datacommons.cancer.gov`:

- **REST** — Swagger UI at
  [`/v3/docs`](https://api.imaging.datacommons.cancer.gov/v3/docs), OpenAPI at `/v3/openapi.json`.
  ```bash
  curl -s https://api.imaging.datacommons.cancer.gov/v3/cohort/manifest \
    -H 'content-type: application/json' \
    -d '{"filters": {"terms": {"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}}, "page_size": 3}'
  ```
- **MCP** — add it as a remote/custom connector in Claude (or any spec-conformant MCP client) at
  `https://api.imaging.datacommons.cancer.gov/mcp`. No API key, no config file — point the client
  at the URL and ask it to find and describe an imaging cohort.

See the [User Guide](docs/user-guide.md#connecting-to-the-hosted-mcp-server) for connector setup
and the request/session details, and [`dev/deployment.md`](dev/deployment.md) for the deployment
architecture (dev/test/prod tiers, DNS, autoscaling).

## Documentation

- **[User Guide](docs/user-guide.md)** — concepts, the query surfaces and how they relate, the
  recommended workflow, and worked REST/MCP examples. **Start here to learn how to use it.**
- [`dev/architecture.md`](dev/architecture.md) — internal design (one core, two adapters).
- [`dev/api_v3_plan.md`](dev/api_v3_plan.md) — full design rationale + SQL threat model.
- [`dev/deployment.md`](dev/deployment.md) — Cloud Run deployment.
- [`dev/developer_guide.md`](dev/developer_guide.md) — codebase tour / adding capabilities.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branching, commits, changelog, versioning, releases.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed for callers, per release.
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
uv run idc-api                                       # REST API → http://127.0.0.1:8000 (Swagger at /v3/docs)
uv run idc-mcp                                       # MCP server, stdio (local)
uv run idc-mcp --http --host 0.0.0.0 --port 8080     # MCP server, hosted/shared
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

## Acknowledging IDC

Built on the [NCI Imaging Data Commons (IDC)](https://imaging.datacommons.cancer.gov/). When you
publish results using IDC data, include the per-dataset citations **and** acknowledge IDC itself
by citing the IDC paper — Fedorov et al., [10.1148/rg.230180](https://doi.org/10.1148/rg.230180).
The `citations` capability returns both; see the
[User Guide](docs/user-guide.md#6-licenses--citations).

This software is maintained in part by the NCI Imaging Data Commons project, which has been funded
in whole or in part with Federal funds from the National Cancer Institute, National Institutes of
Health, Department of Health and Human Services, under task order no. HHSN26110071 under contract
no. HHSN261201500003I. The statements do not necessarily reflect the views or policies of the
Department of Health and Human Services, nor does mention of trade names, commercial products, or
organizations imply endorsement by the U.S. Government.
