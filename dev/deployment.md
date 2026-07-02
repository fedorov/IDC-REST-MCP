# IDC API — Deploying to Cloud Run

The service is **stateless** and needs **no secrets, GCP data access, or external
database**: the read-only DuckDB file is baked into the image from the bundled `idc-index`
Parquet (see [Dockerfile](../Dockerfile)). That makes Cloud Run a natural fit —
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

**Cloud Build (no local Docker):**

```bash
gcloud builds submit --config dev/cloudbuild.yaml --substitutions _IMAGE="$IMAGE"
```

**Or local Docker:**

```bash
gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker build -t "$IMAGE" .
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

### Rate limiting / abuse protection

`--allow-unauthenticated` means anyone can call `run_sql`/manifest endpoints; `--concurrency`
and `--max-instances` above bound the *total* damage (cost, availability) a burst of traffic can
do, but they are not a per-caller rate limit — one abusive IP can still consume the whole
`--max-instances` budget and starve everyone else. Each query is already capped (statement
timeout, row limits — see `IDC_API_SQL_*` above), but many queries at once still hurt. If abuse
becomes a real concern, add a per-IP rate limit **at the edge**, not in the app:

- **Cloud Armor** (attach to a Cloud Run + external Application Load Balancer setup) — rate-based
  bans per IP, the standard GCP-native option.
- **API Gateway / Apigee** in front of Cloud Run — if you also want API keys or quotas.

Both sit in front of the container and need no code change. The structured request/tool-call
logs (`idc_api.rest` / `idc_api.mcp` loggers, shipped to Cloud Logging automatically) are the
signal to watch for "is someone abusing this" before reaching for either.

## 4. Verify

```bash
URL=$(gcloud run services describe idc-api-v3 --region "$REGION" --format='value(status.url)')
curl -s "$URL/health"; echo
curl -s "$URL/v3/version"; echo
open "$URL/docs"   # Swagger UI
```

> **Don't use `/healthz` as a health-check path on Cloud Run's default `*.run.app` domain.**
> Google's front end reserves that exact path and returns its own generic 404 page for it
> before the request ever reaches your container — a well-known Cloud Run gotcha (other
> frameworks, e.g. Streamlit, have hit the same thing). The app exposes `/health` instead.

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
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2,IDC_API_BUILD=$(git rev-parse --short HEAD)
```

The MCP endpoint is then `https://<service-url>/mcp` (note the `/mcp` path).

> **Which build is live?** The MCP `initialize` handshake reports `serverInfo.version`. Left
> unset it would echo the MCP SDK's own version, so set `IDC_API_BUILD` (above) to a short git
> SHA: the server appends it to the package version as a PEP 440 local segment (e.g.
> `3.0.0.dev0+a1b2c3d`), giving a version string that moves on every redeploy. Read it back with
> a `tools/list`-less initialize probe:
>
> ```bash
> curl -s -X POST "$URL/mcp" -H 'Content-Type: application/json' \
>   -H 'Accept: application/json, text/event-stream' \
>   -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
>   | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'
> ```

> **Host-header / DNS-rebinding protection.** The MCP streamable-HTTP transport ships with
> DNS-rebinding protection that allow-lists the `Host` header to localhost only, which would
> reject a Cloud Run domain with **HTTP 421 "Invalid Host header."** Because this service is
> public, unauthenticated, and read-only, that protection is **disabled by default**
> (`mcp_dns_rebinding_protection=False`; see [settings.py](../src/idc_api/settings.py)), so the
> hosted endpoint works out of the box. To re-enable it, set
> `IDC_API_MCP_DNS_REBINDING_PROTECTION=true` and
> `IDC_API_MCP_ALLOWED_HOSTS=["your-host"]` (JSON). If you still see a 421, you're running an
> image built before this default — rebuild and redeploy.

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
- **CI/CD:** [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) automates
  steps 2–3 as a manual (`workflow_dispatch`) job — pick `rest`, `mcp`, or `both` and it
  builds, pushes, and deploys. It authenticates with a long-lived service account key rather
  than Workload Identity Federation, so set two repo secrets first:
  - `GCP_PROJECT_ID` — the target project ID.
  - `GCP_SA_KEY` — a JSON key for a deploy service account with:
    - `roles/run.admin` (deploy/update Cloud Run services)
    - `roles/artifactregistry.writer` (push images to the repo created in step 1)
    - `roles/cloudbuild.builds.editor` (submit Cloud Build jobs)
    - `roles/iam.serviceAccountUser` (act as the Cloud Run runtime service account)
    - `roles/storage.admin` (`gcloud builds submit` calls `storage.buckets.get` on the
      auto-created `<PROJECT_ID>_cloudbuild` GCS bucket before uploading source — `roles/
      storage.objectAdmin` covers object read/write but *not* that bucket-level check, and
      is the one role here that's easy to under-scope; without `storage.admin` you'll hit
      "user is forbidden from accessing the bucket" even with every other role granted)
    - `roles/serviceusage.serviceUsageConsumer` (base `serviceusage.services.use`
      permission needed to call any API as this project; Owner/Editor include it
      implicitly, this narrow role set doesn't)

  Create it once with:

  ```bash
  gcloud iam service-accounts create idc-api-deployer --display-name="IDC API deployer"
  SA="idc-api-deployer@$PROJECT_ID.iam.gserviceaccount.com"
  for role in roles/run.admin roles/artifactregistry.writer roles/cloudbuild.builds.editor \
              roles/iam.serviceAccountUser roles/storage.admin \
              roles/serviceusage.serviceUsageConsumer; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role"
  done
  gcloud iam service-accounts keys create sa-key.json --iam-account="$SA"
  ```

  IAM bindings on Cloud Storage can take a minute or two to propagate — if `buckets.get`
  still fails right after granting the role, wait ~60s and retry before assuming the role is
  wrong.

  Paste `sa-key.json`'s contents into the `GCP_SA_KEY` secret, then delete the local file. If
  you'd rather avoid a long-lived key, swap the workflow's `google-github-actions/auth@v2` step
  for Workload Identity Federation — the roles above stay the same.
