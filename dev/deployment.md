# IDC API v3 — Deploying to Cloud Run

The v3 service is **stateless** and needs **no secrets, GCP data access, or external
database**: the read-only DuckDB file is baked into the image from the bundled `idc-index`
Parquet (see [Dockerfile.v3](../Dockerfile.v3)). That makes Cloud Run a natural fit —
scale-to-zero, one container, public unauthenticated access.

This guide covers the **REST API**. The optional remote **MCP** service is at the end.

## Prerequisites

- `gcloud` CLI authenticated (`gcloud auth login`) and a project with billing enabled.
- Roles to deploy: `roles/run.admin`, `roles/artifactregistry.admin` (or writer),
  `roles/cloudbuild.builds.editor`, and `roles/iam.serviceAccountUser`.

Set shimmable variables for the commands below:

```bash
export PROJECT_ID=your-project
export REGION=us-central1
export REPO=idc                       # Artifact Registry repo name
export IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/idc-api-v3:latest
gcloud config set project "$PROJECT_ID"
```

## 1. One-time project setup

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="IDC API images"
```

## 2. Build & push the image

Because the Dockerfile is named `Dockerfile.v3` (the default build flow only finds
`Dockerfile`), use either path:

**Cloud Build (no local Docker):**

```bash
gcloud builds submit --config dev/cloudbuild.v3.yaml --substitutions _IMAGE="$IMAGE"
```

**Or local Docker:**

```bash
gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker build -f Dockerfile.v3 -t "$IMAGE" .
docker push "$IMAGE"
```

## 3. Deploy the REST API

```bash
gcloud run deploy idc-api-v3 \
  --image "$IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --concurrency 40 \
  --min-instances 0 --max-instances 5 \
  --cpu-boost \
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2
```

`--allow-unauthenticated` is correct here — all IDC data is open. Cloud Run injects `PORT`
(8080); the container already listens on `0.0.0.0:$PORT`. The default compute service account
is fine — the service needs **no** GCP permissions (no BigQuery/GCS access; downloads are
client-side). For least privilege you may attach a dedicated SA with no roles via
`--service-account`.

### ⚠️ Sizing: DuckDB memory must fit the container

`IDC_API_DUCKDB_MEMORY_LIMIT` defaults to **4GB**. If that exceeds the Cloud Run memory the
container will be OOM-killed under load. **Always set it below `--memory`, leaving headroom for
Python/uvicorn:**

| `--memory` | set `IDC_API_DUCKDB_MEMORY_LIMIT` | `IDC_API_DUCKDB_THREADS` |
|---|---|---|
| `2Gi` | `1200MB` | match `--cpu` (e.g. `1`–`2`) |
| `4Gi` | `3GB` | `2` |

Set `IDC_API_DUCKDB_THREADS` ≈ `--cpu` so concurrent requests don't oversubscribe the CPU
(each query is already capped to this many threads). Other tunables:
`IDC_API_SQL_MAX_ROWS`, `IDC_API_SQL_TIMEOUT_SECONDS`, `IDC_API_MANIFEST_HARD_CAP`
(see [settings.py](../src/idc_api/settings.py)). Leave `IDC_API_DUCKDB_PATH` as baked.

> Avoid setting `IDC_API_CORS_ALLOW_ORIGINS` via `--set-env-vars`: it's a list and
> pydantic-settings expects JSON (`["https://app.example.com"]`), which is awkward to quote in
> gcloud. The default `["*"]` is appropriate for an open API.

## 4. Verify

```bash
URL=$(gcloud run services describe idc-api-v3 --region "$REGION" --format='value(status.url)')
curl -s "$URL/healthz"; echo
curl -s "$URL/v3/version"; echo
open "$URL/docs"   # Swagger UI
```

## 5. Updating for a new IDC release

The image bakes whatever `idc-index-data` resolves at build time. To publish a new IDC version,
**rebuild and redeploy** (steps 2–3). For reproducibility, pin the version in
[pyproject.toml](../pyproject.toml) (e.g. `idc-index==0.12.2`, which pulls a specific
`idc-index-data`) so a rebuild is deterministic; bump the pin to move IDC versions. The running
version is always reported at `/v3/version`.

## Optional: remote MCP service (HTTP)

Deploy the same image with the MCP command to expose the tools over MCP streamable-http
(download is disabled in hosted mode — manifests/URLs only):

```bash
gcloud run deploy idc-mcp-v3 \
  --image "$IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --min-instances 0 --max-instances 5 \
  --command idc-mcp \
  --args=--http,--host,0.0.0.0,--port,8080 \
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2
```

The MCP endpoint is then `https://<service-url>/mcp`.

> **Stateless by design.** The MCP HTTP transport is configured stateless
> (`stateless_http=True`, `json_response=True` in [mcp/server.py](../src/idc_api/mcp/server.py)),
> so each request is self-contained and the service **autoscales across instances like the REST
> API** — no session affinity or single-instance pin needed. This is safe because the server
> exposes only client-initiated tools + static resources (no server→client sampling,
> elicitation, subscriptions, or streamed progress, which are the only things that would need a
> persistent session). The local **stdio** MCP remains the primary path for end users — it's the
> only mode that can download files to the user's machine.

## Notes

- **Cost:** with `--min-instances 0` the service scales to zero and costs nothing idle; cold
  starts are fast because the DuckDB file is prebuilt in the image.
- **Custom domain / CDN:** map a domain via Cloud Run domain mappings, or front it with a load
  balancer + Cloud CDN. Discovery responses change only per IDC release, so they cache well —
  add `Cache-Control` if you put a CDN in front. See [caching_and_cdn.md](caching_and_cdn.md)
  for a primer and the proposed (not-yet-implemented) cache-header enhancement.
- **CI/CD:** wire steps 2–3 into a pipeline (Cloud Build trigger on tag, or extend the GitHub
  Actions workflow with a deploy job using Workload Identity Federation).
