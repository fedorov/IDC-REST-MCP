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
  --set-env-vars IDC_API_DUCKDB_MEMORY_LIMIT=3GB,IDC_API_DUCKDB_THREADS=2,IDC_API_BUILD=$(git rev-parse --short HEAD)
```

> `IDC_API_BUILD` (a short git SHA / image tag) is stamped into the software version reported at
> `GET /v3/version` (`build`), `GET /`, and OpenAPI `info.version`, so you can confirm which build
> a hosted REST instance is running — the same mechanism the MCP service uses for
> `serverInfo.version` (see *Which build is live?* below). Omit it and `/v3/version` reports
> `build: null` (the package version alone, static across redeploys of a release).

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

> **HSTS.** NCI security policy requires `Strict-Transport-Security` on every response, and the
> application injects it (Cloud Run terminates TLS but adds no security headers). The default
> max-age is the policy value of one year, so a plain deploy is compliant; the deploy workflow
> sets `IDC_API_HSTS_MAX_AGE=3600` on **dev/test** so a misconfigured deploy can't lock browsers
> out of the domain for a year, and the year on prod. Both the REST app and the hosted MCP
> transport send it.

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
curl -s "$URL/v3/health"; echo
curl -s "$URL/v3/version"; echo
open "$URL/v3/docs"   # Swagger UI

# Exercise every documented OpenAPI example against the deployment (no dependencies):
python3 dev/smoke_openapi_examples.py "$URL"
```

> **Post-deploy checks run automatically on every tier.** The reusable
> [deploy.yml](../.github/workflows/deploy.yml) runs `/v3/health` + `/v3/version` + HSTS assertions
> (against the direct `*.run.app` URL, to prove the container itself came up) and then
> `dev/smoke_openapi_examples.py`, which reads the deployed `/v3/openapi.json` and fires a request
> built from each declared example — so a documented value that no longer resolves (a removed
> StudyInstanceUID, a stale example) fails the deploy rather than quietly misleading Swagger-UI
> users. The example smoke test targets the tier's **`PUBLIC_BASE_URL`** — the public domain behind
> the load balancer (the surface real users hit; see *Tier URLs — custom domains* and *Shared-domain
> path routing*), and it first asserts `/v3/version` responds from Cloud Run (`Server: Google
> Frontend`) to catch a down public URL or a URL-map/ESP routing regression. **`PUBLIC_BASE_URL` is
> required**: if it is unset, or the public URL isn't serving, the step fails — it does **not** fall
> back to the `*.run.app` URL, because that would skip exactly the user-facing surface this test
> exists to cover. (So attach a tier's domain and set `PUBLIC_BASE_URL` before its first CI deploy.)
> Because it runs against real tier data it can go red when IDC re-releases and an example UID is
> retired; fix the example, or set the `SMOKE_SOFT_FAIL` Environment variable to downgrade that tier
> to a warning.

> **Don't use `/healthz` as a health-check path on Cloud Run's default `*.run.app` domain.**
> Google's front end reserves that exact path and returns its own generic 404 page for it
> before the request ever reaches your container — a well-known Cloud Run gotcha (other
> frameworks, e.g. Streamlit, have hit the same thing). The app's health check is `/v3/health`
> (all REST routes live under `/v3` — see *Shared-domain path routing*), which also sidesteps any
> reserved root path.

## 5. Updating for a new IDC release

The image bakes whatever `idc-index-data` resolves at build time. To publish a new IDC version,
**rebuild and redeploy** (steps 2–3). For reproducibility, pin the version in
[pyproject.toml](../pyproject.toml) (e.g. `idc-index==0.12.4`, which pulls a specific
`idc-index-data`) so a rebuild is deterministic; bump the pin to move IDC versions. The running
version is always reported at `/v3/version`.

## Optional: remote MCP service (HTTP)

Deploy the same image with the MCP command to expose the tools over MCP streamable-http
(retrieval is manifests/URLs only — the server never transfers files):

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
>
> The **REST** service reports the same software version with no handshake needed: `GET /v3/version`
> returns `api_version` + `build`, and the combined string is also the OpenAPI `info.version` at
> `/v3/openapi.json` (and `server_version` at `GET /v3`).

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
> persistent session).

## Notes

- **Cost:** with `--min-instances 0` the service scales to zero and costs nothing idle; cold
  starts are fast because the DuckDB file is prebuilt in the image.
- **Custom domain / CDN:** map a domain via Cloud Run domain mappings, or front it with a load
  balancer + Cloud CDN. Discovery responses change only per IDC release, so they cache well —
  add `Cache-Control` if you put a CDN in front. See [caching_and_cdn.md](caching_and_cdn.md)
  for a primer and the proposed (not-yet-implemented) cache-header enhancement. For the specific
  per-tier public URLs (the `*.canceridc.dev` / `api.imaging.datacommons.cancer.gov` domains
  carried over from the legacy API) and how to attach them, see
  [Tier URLs — custom domains](#tier-urls--custom-domains) below.
- **CI/CD:** deployment is automated across dev / test / prod tiers with GitHub
  Actions — see [CI/CD: dev / test / prod tiers](#cicd-dev--test--prod-tiers) below.
  The manual steps 2–3 above remain the ground truth for what those workflows run and for
  first-time / out-of-band deploys.

## CI/CD: dev / test / prod tiers

Three GitHub Actions workflows share one reusable deploy job and use **GitHub Environments**
(`dev`, `test`, `prod`) for per-tier config and governance. Each tier is a **separate GCP
project** — matching the legacy IDC-API (CircleCI) convention of one project per tier.

**Image flow — each tier in its own project; prod runs exactly what test validated.** Because the
read-only DuckDB index is baked into the image *at build time*, images stay inside the security
boundary and never cross it the wrong way:

- **dev** is *outside* the boundary and self-contained: it builds its own image into the **dev**
  project's registry and deploys it. Nothing dev produces is ever promoted onward.
- **test** builds the **canonical** image into the **test** project's registry and deploys it
  (test doubles as UAT). Pinning `idc-index` in [pyproject.toml](../pyproject.toml) keeps that
  build deterministic.
- **prod** deploys **test's exact image by immutable `@sha256:` digest**, referencing test's
  registry directly — no rebuild, no copy — so prod runs the bytes test validated. prod's deployer
  SA and Cloud Run service agent are granted read on test's Artifact Registry (the only
  cross-project access in the design).

### Coming from the CircleCI pipeline?

If you maintained the legacy `.circleci/config.yml`, here is the mental-model mapping. The two
things that move the most: **tier selection** and **the approval gate** both leave the pipeline
file — the first becomes the *trigger*, the second becomes an *Environment setting*.

| Legacy CircleCI | Here (GitHub Actions) |
|---|---|
| Branch name picks the tier (`idc-prod` / `idc-uat` / `idc-test` / `master`) | The **trigger** picks the tier: push `main` → dev, manual dispatch → test, `v*` tag → prod |
| Per-tier secrets as context env vars (`DEPLOYMENT_*_IDC_<TIER>`) | Per-tier **Environment** Secrets/Variables (`GCP_PROJECT_ID`, `GCP_SA_KEY`, sizing) |
| A `type: approval` hold **job in `config.yml`** gates the deploy | A **Required reviewers** rule on the `prod` **Environment**, set in repo *Settings* — **not** in any YAML (see [Configuring the prod approval gate](#configuring-the-prod-approval-gate)) |
| Deploy auto-runs on every matching branch | dev auto-runs; test is manual; prod waits for reviewer approval |
| Rebuild per branch | dev and test each build in their own project; prod runs test's exact image by digest |

| Workflow | Trigger | Result |
|---|---|---|
| [build-and-deploy-dev.yml](../.github/workflows/build-and-deploy-dev.yml) | push to `main` (image paths) or manual | build in the dev project + deploy **dev** |
| [promote.yml](../.github/workflows/promote.yml) (dispatch) | manual, pick a git ref | build the canonical image in test + deploy **test** |
| [promote.yml](../.github/workflows/promote.yml) (tag) | push a `v*` tag | deploy test's digest to **prod** (behind the required-reviewer gate) |
| [deploy.yml](../.github/workflows/deploy.yml) | reusable (`workflow_call`) | the shared deploy job the callers invoke |

### Cutting a release

Versioning policy — what a version *number* means, and when to bump which part — is in
[CONTRIBUTING.md](../CONTRIBUTING.md#versioning). This section is the mechanics.

> [!IMPORTANT]
> **Pushing a `v*` tag deploys to production**, and the glob matches pre-release tags too —
> `v3.0.0b1` goes to prod exactly like `v3.0.0`. Never create a `v*` tag for bookkeeping, and
> beware `git push --tags` firing a deploy from a stale local tag.

Two constraints fall out of the security boundary above — prod runs test's exact bytes and never
rebuilds:

1. **A `v*` tag must point at a commit already promoted to test** (its image exists in test's
   registry). Cut releases from a ref you've dispatched to test; tag a commit that never went
   through test and the prod deploy fails fast at the digest-resolve step.
2. **The version bump must be its own commit, and it must go through test.** The version comes from
   the installed package metadata, baked into the image when *test* builds it; `IDC_API_BUILD` only
   stamps the git SHA on top. Tagging `v3.0.0` on the same commit that shipped as `3.0.0b1` would
   redeploy an image that still reports `3.0.0b1` at `/v3/version`. `promote.yml` guards this: on a
   `v*` tag it asserts `tag == "v" + pyproject version` and fails **before** the reviewer gate if
   they disagree.

#### Steps

1. **Bump and curate.** In one PR: set `version` in [pyproject.toml](../pyproject.toml), and in
   [CHANGELOG.md](../CHANGELOG.md) rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`, open a
   fresh empty `[Unreleased]`, and update the link definitions at the foot of the file. Merge it.
2. **Promote to test.** Run [promote.yml](../.github/workflows/promote.yml) via workflow dispatch
   against that merge commit. It builds the canonical image into test's registry and deploys
   `testing-api.canceridc.dev`.
3. **Verify** against test — `/v3/health`, `/v3/version` (confirm it reports the version you just
   set), and the MCP handshake at `/mcp`.
4. **Tag.** `git tag -a v3.0.0 -m "v3.0.0" <that commit> && git push origin v3.0.0`. This starts the
   prod deploy, which waits on the `prod` Environment's required-reviewer gate.
5. **Approve** the deployment, then confirm `api.imaging.datacommons.cancer.gov/v3/version`.
6. **Publish a GitHub Release** on the tag, with the changelog section as its body. Tick **"Set as a
   pre-release"** for `bN` / `rcN` tags.

#### The v3 beta

v3 ships to production as `3.0.0b1` before `3.0.0`.

The beta is **not** a traffic-safety measure — it can't be one. The prod load balancer routes only
`/v3/*` to the `idc-api-v3` service (see [Shared-domain path routing](#shared-domain-path-routing-as-deployed--the-glob-gotcha)
below); every other path falls through to the legacy ESP backend. No existing caller reaches v3, so
shipping it cannot break them. What the beta buys is the freedom to **change the `/v3` contract in
response to real usage** without spending a major version — which, under the versioning policy,
would otherwise cost a whole new `/v4` prefix — plus an honest signal to early adopters that the
surface may move. Exit the beta by tagging `v3.0.0` once the contract has held under real use.

Deliberately **not** doing a Cloud Run traffic split (`--tag beta --no-traffic` plus a percentage
rollout) for this release. There is no incumbent v3 revision to canary against, and percentage
splits are applied **per request**, not per session — an MCP streamable-http session could have its
requests land on different revisions mid-conversation unless `--session-affinity` is enabled. If a
canary becomes worthwhile for a later release (`3.1.0` onward, once v3 has consumers), use a
**tagged revision** at zero traffic, which gets its own `beta---idc-api-v3-*.run.app` URL that
testers opt into explicitly, rather than a percentage split of the live domain.

### One-time setup

Each tier is its own GCP project with its own **GitHub Environment** (`dev`, `test`, `prod`). The
per-tier **deployer** service accounts already exist — their JSON keys live in each tier's
deployment bucket, so reuse them rather than creating new ones. The specific service accounts are
tracked in project-management issue 2068.

**1. Per-tier GitHub Environment.** Create `dev`, `test`, and `prod`; for each add:

- **Secrets**
  - `GCP_PROJECT_ID` — that tier's project ID.
  - `GCP_SA_KEY` — the tier's existing **deployer** SA key (from the tier's deployment bucket).
- **Variables** (all optional; defaults in parentheses)
  - `RUNTIME_SA` — the tier's dedicated Cloud Run **runtime** SA (e.g.
    `cloud-run-sa@<project>.iam.gserviceaccount.com`), passed via `--service-account`. Omit and the
    service runs as the default compute SA (acceptable for dev, not recommended for prod).
  - `REGION` (`us-central1`), `AR_REPO` (`idc`), `CPU` (`2`), `MEMORY` (`4Gi`), `CONCURRENCY`
    (`40`), `MIN_INSTANCES` (`0`), `MAX_INSTANCES` (`5`), `DUCKDB_MEMORY_LIMIT` (`3GB`),
    `DUCKDB_THREADS` (`2`). Run prod hotter (e.g. `MIN_INSTANCES=1`).
- On **`prod`** only: add the **Required reviewers** approval gate — see
  [Configuring the prod approval gate](#configuring-the-prod-approval-gate).

**Deployer vs runtime SA.** `GCP_SA_KEY` is the **deployer** and must be able to run
`gcloud run deploy` (`roles/run.admin`, `roles/iam.serviceAccountUser` to act as the runtime SA);
tiers that build (dev, test) additionally need `roles/artifactregistry.writer`,
`roles/cloudbuild.builds.editor`, and `roles/storage.admin` (the last because `gcloud builds
submit` calls `storage.buckets.get` on the auto-created `<PROJECT_ID>_cloudbuild` bucket, which
`roles/storage.objectAdmin` does *not* cover), plus `roles/serviceusage.serviceUsageConsumer`. The
**runtime** SA (`RUNTIME_SA`) is what the container runs as and needs **no roles** — the app makes
no GCP calls.

**2. Registry access for prod — the only cross-project grant.** dev and test build into and deploy
from their **own** project's registry, so they need no cross-project access. prod runs **test's**
image, so on **test's** Artifact Registry grant:

- prod's **deployer SA** `roles/artifactregistry.reader` (to resolve the digest at deploy), and
- prod's Cloud Run **service agent**
  (`service-<PROD_PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com`)
  `roles/artifactregistry.reader` (to pull the image at deploy / cold start).

```bash
# Run against the TEST project. TEST_AR_REPO defaults to "idc", TEST_REGION to "us-central1".
gcloud artifacts repositories add-iam-policy-binding "$TEST_AR_REPO" \
  --project "$TEST_PROJECT_ID" --location "$TEST_REGION" \
  --member="serviceAccount:<prod-deployer-sa>" --role=roles/artifactregistry.reader
gcloud artifacts repositories add-iam-policy-binding "$TEST_AR_REPO" \
  --project "$TEST_PROJECT_ID" --location "$TEST_REGION" \
  --member="serviceAccount:service-<PROD_PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

The prod path also needs repo-level **Variables** locating test's registry: `TEST_PROJECT_ID`
(required), plus `TEST_REGION` / `TEST_AR_REPO` if they differ from the defaults.

IAM bindings can take a minute or two to propagate — if a read fails right after granting, wait
~60s and retry before assuming the role is wrong.

### Configuring the prod approval gate

**This is the piece with no `.yml` equivalent — it lives entirely in repo Settings.** In CircleCI
you gated a deploy by adding a `type: approval` hold *job* to `config.yml`. GitHub Actions works
the other way round: the gate is a property of the **Environment**, and the workflow only *opts
in* by naming that environment. There is deliberately **no way to require reviewers from the
workflow file** — so a pull request can't weaken it. All [deploy.yml](../.github/workflows/deploy.yml)
does is declare `environment: prod` on its deploy job; the rule itself you set here, once:

1. Repo → **Settings → Environments**. Create an environment named exactly **`prod`** if it
   doesn't exist (the name must match what [promote.yml](../.github/workflows/promote.yml) passes
   on a `v*` tag).
2. Tick **Required reviewers** and add the users/teams allowed to approve prod deploys (up to 6).
   Optionally also tick **Prevent self-review** so the person who cut the tag can't approve their
   own deploy.
3. *(Optional)* Under **Deployment branches and tags** choose **Selected** and add the `v*`
   pattern, so only tag-triggered runs can ever target `prod`.
4. **Save protection rules.**

**What this looks like at deploy time.** Pushing a `v*` tag starts `promote.yml`: the `resolve`
job runs, then the reusable deploy job — bound to `prod` — **pauses before any deploy step**, in a
*"Waiting — review required"* state. A designated reviewer opens the run in the **Actions** tab,
clicks **Review deployments → prod → Approve and deploy** (or Reject). Only on approval do the
digest-resolve and `gcloud run deploy` steps run — and the `prod` secrets aren't exposed to the
job until then either. dev and test have no such rule, so they deploy without a pause. GitHub also
records who approved each prod deployment under the repo's **Deployments** view.

> ⚠️ **The gate is opt-in and off by default.** If you skip this — e.g. the `prod` environment
> exists but has no Required-reviewers rule — `environment: prod` still resolves and the prod
> deploy runs **unattended**. The YAML cannot enforce the gate; only this setting does. (Required
> reviewers are free on **public** repos, which this is; on private/internal repos they need
> GitHub Pro/Team/Enterprise.)

> **Long-lived keys vs WIF.** These workflows authenticate with JSON service-account keys
> (`google-github-actions/auth@v3` + `credentials_json`). To avoid long-lived keys, swap each
> `auth` step for Workload Identity Federation — the roles above are unchanged; only the `auth`
> step's inputs differ.

### Tier URLs — custom domains

The legacy pipeline set each tier's URL through **Cloud Endpoints**: the `host:` field in the
staged OpenAPI spec *was* the API's public domain (`gcloud endpoints services deploy`). Cloud Run
has **no Endpoints layer** — a freshly deployed service answers only on a Google-assigned
`https://idc-api-v3-<project-number>.<region>.run.app`. To keep serving on the same public domains,
attach a **custom domain** to each tier's Cloud Run service. The domains carry over one-to-one; the
legacy `uat` tier has no successor (the 4-tier CircleCI setup became 3 tiers here):

| New tier | Public URL (custom domain) | Legacy Cloud Endpoints `host:` |
|---|---|---|
| dev  | `https://dev-api.canceridc.dev`              | `dev-api.canceridc.dev` |
| test | `https://testing-api.canceridc.dev`          | `testing-api.canceridc.dev` |
| prod | `https://api.imaging.datacommons.cancer.gov` | `api.imaging.datacommons.cancer.gov` |
| ~~uat~~ | — retired (merged into the tiers above) | a `*.canceridc.dev` host |

There are two ways to attach a domain; choose per tier:

- **(A) Cloud Run domain mapping** — one `gcloud` command + DNS records, Google-managed TLS. The
  simplest path, documented step-by-step below.
- **(B) External Application Load Balancer + serverless NEG** — more setup, but the only option
  that also enables **Cloud Armor** per-IP rate limiting and **Cloud CDN** (see *Rate limiting /
  abuse protection* and the *Custom domain / CDN* note). **Recommended for `prod`**, the tier most
  exposed to abuse; outlined at the end.

Attaching a domain is **one-time infrastructure setup**, run out-of-band by an operator with access
to that tier's project — it is **not** part of the CI deploy. The mapping points at the *service
name*, not at any revision, so every subsequent CI promotion/rollback keeps the same domain and
nothing in the workflows changes.

Once a tier's domain is live, set it as the `PUBLIC_BASE_URL` variable on that tier's GitHub
Environment (the values in the table above) so the post-deploy example smoke test exercises the
public surface rather than the `*.run.app` URL — see *§4 Verify*.

#### (A) Domain mapping — step by step (one-time per tier)

**Before you start, per tier:**

- The tier's service must already be deployed (CI has run at least once) so it exists in that
  project + region.
- You must be able to **(1) verify domain ownership** to Google *and* **(2) edit the domain's DNS**:
  - `canceridc.dev` (dev, test) — owned by IDC/ISB, so both are self-service.
  - `datacommons.cancer.gov` (prod) — a **NCI-controlled** DNS zone. The ownership-verification
    record **and** the final DNS record for `api.imaging.datacommons.cancer.gov` must be filed with
    **NCI**; this is not self-service and has lead time, so plan the prod cutover around it.
- Domain mappings aren't offered in every Cloud Run region; `us-central1` (this repo's default
  `REGION`) is supported. If a tier runs in a region without mapping support, use **(B)**.

**Steps** (shown for `dev`; repeat with each tier's domain, project, and region — the `$GCP_PROJECT_ID`
and `$REGION` for a tier are the same values its GitHub Environment uses):

1. **Verify the registrable domain once** (per Google account/project) to prove you own
   `canceridc.dev` / `cancer.gov`:
   ```bash
   gcloud domains verify canceridc.dev     # opens Search Console; add the TXT record it prints
   ```
   For `cancer.gov`, **NCI** must add the verification record (or grant the deploying account
   ownership in Search Console) — request it from them.

2. **Create the mapping** in that tier's project + region:
   ```bash
   gcloud run domain-mappings create \
     --service idc-api-v3 \
     --domain  dev-api.canceridc.dev \
     --region  "$REGION" \
     --project "$GCP_PROJECT_ID"
   ```

3. **Read back the DNS records** Google wants published, then add them at the domain's DNS provider:
   ```bash
   gcloud run domain-mappings describe \
     --domain dev-api.canceridc.dev --region "$REGION" --project "$GCP_PROJECT_ID" \
     --format='value(status.resourceRecords[].name, status.resourceRecords[].type, status.resourceRecords[].rrdata)'
   ```
   A subdomain (all three of ours are subdomains) gets a **CNAME → `ghs.googlehosted.com.`**. Add it
   in the `canceridc.dev` zone yourself; for `api.imaging.datacommons.cancer.gov`, file the CNAME
   with **NCI**.

4. **Wait for the managed TLS cert.** Provisioning starts once the DNS record resolves and takes
   minutes to ~24h. Watch it:
   ```bash
   gcloud run domain-mappings describe \
     --domain dev-api.canceridc.dev --region "$REGION" --project "$GCP_PROJECT_ID" \
     --format='value(status.conditions[].type, status.conditions[].status)'
   ```

5. **Verify** — the same checks as the `*.run.app` URL, now on the real domain:
   ```bash
   curl -s https://dev-api.canceridc.dev/v3/health;  echo
   curl -s https://dev-api.canceridc.dev/v3/version; echo
   ```
   For the MCP service, map a domain to `idc-mcp-v3` the same way and test the `…/mcp` path.

#### (B) Load balancer — outline (recommended for `prod`)

Use this when you also want Cloud Armor rate limiting or Cloud CDN. The domain's DNS points at the
load balancer's IP (not at Cloud Run directly):

1. Reserve a **global static IP**.
2. Create a **serverless NEG** for the tier's Cloud Run service and add it to a **backend service**.
3. *(Optional)* attach a **Cloud Armor** rate-limit policy to that backend service.
4. Create a **Google-managed cert** for the domain, a **URL map**, an **HTTPS target proxy**, and a
   **global forwarding rule**.
5. Point the domain's **A/AAAA** records at the reserved IP (for `cancer.gov`, file with **NCI**).
   The cert provisions once DNS resolves; then verify as in (A) step 5.

### Shared-domain path routing (as deployed)

Each tier serves **both** APIs on its one domain: the load balancer's URL map path-routes to two
Cloud Run services. Verified on `dev-api.canceridc.dev` and `testing-api.canceridc.dev` (2026-07):

| Path | → backend (serverless NEG) |
|---|---|
| `/mcp`, `/mcp/*`     | `idc-mcp-v3` (remote MCP endpoint) |
| everything else (default) | `idc-api-v3` (REST API) |

The REST service is the URL map's **default backend**, so it — not the load balancer — decides what
every non-`/mcp` path does. That is why `/` (307 → `/v3/docs`), `/v3` (landing JSON), and an
unmatched `/zzz` (`{"detail":"Not Found"}`) all answer from the app. **Keep new REST routes under
`/v3/`** anyway: the prefix is what lets a future `/v4` be served alongside, and the root redirect is
the one deliberate exception.

**Tell which backend a path reached from the `Server` response header** — the fastest way to check
whether a routing change took effect:

```bash
curl -sI https://dev-api.canceridc.dev/v3/version | grep -i server   # want: Google Frontend
```

- `Server: Google Frontend` → the request reached **Cloud Run directly**. ✅ correct.
- `Server: nginx` + a body of `{"code":5,"message":"Method does not exist.", … "detail":"service_control"}`
  → the request went through the **legacy Cloud Endpoints / ESP** proxy (App Engine-era leftover)
  that used to front the domain. This should no longer happen on any tier; if it reappears, the URL
  map's default backend has been pointed back at the ESP.

> **Resolved (2026-07) — the `/v3/*` glob gotcha and the unreachable bare `/v3`.** The URL map once
> routed only `/v3/*` to the REST NEG and left everything else on a default backend that was still
> the legacy ESP. Two symptoms followed: root-level routes (`/health`, `/docs`, …) returned nginx
> "Method does not exist", fixed by moving the whole REST surface under `/v3`; and bare `/v3` (no
> trailing slash) fell through to the ESP and 404'd, since the glob matched only paths *under*
> `/v3/`. Both are gone now that the **default backend is the `idc-api-v3` NEG** — bare `/v3`
> returns 200 and there is no ESP in any path. Kept here because a URL-map regression would bring
> the exact same symptoms back.

#### Remote MCP over the shared domain — verified behavior

`/mcp` is functional **end-to-end**, not just the handshake — verified on `dev-api.canceridc.dev`:
`initialize`, `tools/list` (18 tools), and `tools/call get_idc_version` (real result) all succeed.
It runs **stateless** (`stateless_http=True`) — no `Mcp-Session-Id`, no session affinity, so it
autoscales like REST. Caveats:

- **Retrieval is manifests/URLs only** on every transport — the server never transfers files;
  callers download directly from the public S3/GCS buckets (`idc` CLI / s5cmd).
- **Both `…/mcp` and `…/mcp/` are served directly** — no redirect either way. FastMCP registers one
  exact-path route at `/mcp`, so out of the box Starlette 307s `/mcp/` onto it; `http_app()` in
  [mcp/server.py](../src/idc_api/mcp/server.py) registers the trailing-slash form as a real route
  and turns `redirect_slashes` off, because making an RPC client replay its POST body across a
  redirect is a bad bet. Either spelling works in a client config.
- **Streaming / LB timeout:** stateless JSON tool calls return immediately, so the LB backend
  timeout (~30s default) is moot today; if a long-running or server-streamed tool is ever added,
  raise the backend-service timeout then.
