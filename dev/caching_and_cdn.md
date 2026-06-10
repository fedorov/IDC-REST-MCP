# Caching & CDNs for IDC API v3 (explainer + future enhancement)

This is both a primer on HTTP caching / CDNs and a record of a **potential future
enhancement**: making the API "CDN-ready" by emitting `Cache-Control`/`ETag` headers. It is
*not implemented yet* — see [Status](#status) at the bottom. Background:
[deployment.md](deployment.md) (the "Custom domain / CDN" note) and
[api_v3_plan.md](api_v3_plan.md) (Phase 2 → "CDN caching").

## What a CDN is

**CDN = Content Delivery Network** — a globally distributed fleet of caching servers that sit
between users and your real server, storing copies of responses close to where users are.

Analogy: your API is one warehouse in Iowa; every user worldwide sends a truck there and back
for each request. A CDN opens hundreds of **local depots** — the first request in Tokyo still
goes to Iowa, but the depot keeps a copy so the next thousand Tokyo users get it from down the
street.

Two terms:
- **Origin** — your real server (here: the Cloud Run service running the FastAPI app).
- **Edge** — the CDN's many caching servers ("points of presence") spread across the globe.

The core mechanic is **caching**: an edge stores a response the first time it's requested, then
serves that copy to everyone else until it expires — without bothering the origin.

## What a CDN buys you

1. **Lower latency** — users hit a nearby edge, not one distant origin (e.g. 20 ms vs 300 ms).
2. **Less origin load** — 10,000 identical requests can become **1** request to your service;
   the rest are served from the edge.
3. **Resilience** — traffic spikes are absorbed at the edge instead of overwhelming the origin.
4. **Egress savings** — bytes served from the edge don't leave your origin repeatedly.

## How caching is controlled

The origin tells caches what they may do via the **`Cache-Control`** response header:

- `Cache-Control: public, max-age=3600` — anyone (incl. a shared CDN) may store this and treat
  it as fresh for 3600 s.
- `s-maxage=86400` — same, but **specifically for shared caches** (CDN), overriding `max-age`
  for them (e.g. browsers 1 h, CDN 1 day).
- `no-store` — never cache (use for per-request/dynamic responses).
- `ETag` + `If-None-Match` — a lighter scheme: the origin fingerprints a response; the next
  request asks "still the same?" and the origin replies **304 Not Modified** (no body) if
  unchanged. Saves bandwidth even when you can't cache for long.

Two more concepts:
- **Cache key** — what the CDN uses to decide "same request?" — usually method + full URL
  (path + query). `…/values?limit=50` and `?limit=100` are different keys.
- **Invalidation** — forcing edges to drop copies before expiry (a "purge"), e.g. when you
  publish new data.

> **Key limitation:** CDNs cache `GET`/`HEAD`, **not `POST`**. POST is treated as an action,
> not a cacheable resource.

## How this maps to IDC API v3

The IDC data version is **baked into the container image** (the read-only DuckDB file is built
at image-build time), so the queryable data only changes when a **new image is deployed**
(every IDC release, ~quarterly). That makes the read-only discovery endpoints close to static
between deploys:

| Endpoint | Method | Changes… | Cacheable? |
|---|---|---|---|
| `/v3/version`, `/v3/stats` | GET | per IDC release (per deploy) | ✅ excellent — long TTL |
| `/v3/collections`, `/v3/collections/{id}` | GET | per IDC release | ✅ excellent |
| `/v3/analysis_results` | GET | per IDC release | ✅ excellent |
| `/v3/attributes`, `/v3/attributes/{a}/values` | GET | per IDC release | ✅ excellent |
| `/v3/tables`, `/v3/tables/{t}` | GET | per IDC release | ✅ excellent |
| `/v3/viewer-url` | GET | deterministic per UID | ✅ good |
| `/v3/cohort/*`, `/v3/sql`, `/v3/licenses`, `/v3/citations` | **POST** | per request (arbitrary filters/SQL) | ❌ not cached |

The win is concentrated on the **GET discovery endpoints**, which is exactly the most-hit,
most-static part of the surface — a disproportionate benefit for little effort.

## Proposed future enhancement

Currently the FastAPI app sets **no** `Cache-Control` headers, so a CDN won't cache usefully.
The enhancement is small and CDN-agnostic (it also helps plain browser/client caching with no
CDN at all):

1. **A response middleware** in [rest/app.py](../src/idc_api/rest/app.py) that adds, on the GET
   discovery routes:
   `Cache-Control: public, max-age=3600, s-maxage=86400` (tune the TTLs), and `no-store` on the
   POST/dynamic routes.
2. **An `ETag` carrying the IDC version** (from `discovery.version()`), so when a new release is
   deployed the fingerprint changes and caches refresh automatically — no manual purge. This is
   the "version in the cache key" pattern and avoids stale data after a deploy.

Sketch:

```python
# Illustrative only — not yet in the codebase.
from starlette.middleware.base import BaseHTTPMiddleware

DISCOVERY_PREFIXES = ("/v3/version", "/v3/stats", "/v3/collections", "/v3/analysis_results",
                      "/v3/attributes", "/v3/tables", "/v3/viewer-url")

class CacheHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.method == "GET" and request.url.path.startswith(DISCOVERY_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=3600, s-maxage=86400"
            response.headers.setdefault("ETag", f'W/"idc-{IDC_VERSION}"')
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response
```

(Honoring `If-None-Match` to return real `304`s is an optional extra; the headers above already
make the API CDN- and browser-cache-friendly.)

## How you'd actually put a CDN in front

- **GCP-native:** External Application Load Balancer → **Cloud CDN** → a serverless NEG pointing
  at the Cloud Run service. Enable Cloud CDN at the load balancer; it honors the `Cache-Control`
  headers above.
- **Third-party:** point your domain at **Cloudflare** or **Fastly**, which proxy/cache in front
  of the Cloud Run URL.
- A plain Cloud Run **domain mapping** is *not* a CDN — it only assigns a hostname, no caching.

## When it's worth it

A CDN is a scaling/latency optimization, not a correctness requirement:
- **Skip it** for low traffic or a regional/internal audience — Cloud Run already autoscales and
  queries are sub-second.
- **Add it** for a global audience, high read volume on discovery endpoints, or to shield the
  origin from spikes.

Emitting the cache headers (the enhancement above) is cheap and worth doing regardless, since it
also benefits browser/client-side caching; standing up an actual CDN is the part to defer until
traffic justifies it.

## Status

- **Not implemented.** The app sets no cache headers today.
- **Tracked as:** a Phase 2 item in [api_v3_plan.md](api_v3_plan.md) ("CDN caching").
- **Effort:** small — one middleware (+ optional `304` handling), no new dependencies.
