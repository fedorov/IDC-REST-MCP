#!/usr/bin/env python3
"""Post-deploy smoke test: exercise every OpenAPI example against a live deployment.

The REST routes declare `examples` in their OpenAPI schema — query/path parameters carry
`schema.examples`, and each POST body `$ref`s a component schema with a schema-level example.
Swagger UI's "Try it out" pre-fills those exact values, and the user guide's worked examples
mirror them. This script fetches `/v3/openapi.json` from a deployed instance, builds a real
request for every operation that has enough declared examples to do so, fires it, and fails if
the response isn't a healthy one.

Why this exists: the existing post-deploy verify only hits `/v3/health` and `/v3/version`, so
nothing exercises the example-bearing endpoints — which is how a documented example UID that no
longer resolved once shipped (the `viewer-url` example pointed at a StudyInstanceUID absent from
IDC). This closes that gap by exercising every declared example. The pass/fail rule is 2xx **and**
not an `{"error": {...}}` envelope: semantic failures currently carry a proper status (404/400),
but the envelope check is cheap insurance against an endpoint ever returning 200 with an error.

It runs against real deployed data, so it also catches example values that go stale when IDC
re-releases data (series removed between versions). That brittleness is the point — it is an
early-warning signal a unit test against a fixture DB cannot give.

Usage:
    python dev/smoke_openapi_examples.py https://api.imaging.datacommons.cancer.gov
    SMOKE_BASE_URL=... python dev/smoke_openapi_examples.py

Environment:
    SMOKE_BASE_URL    Base URL (if not passed as argv[1]).
    SMOKE_TIMEOUT     Per-request timeout, seconds (default 30).
    SMOKE_MAX_BYTES   Cap on bytes read per response (default 2_000_000) so a whole-collection
                      manifest.txt doesn't pull tens of MB into CI.
    SMOKE_SKIP        Comma-separated substrings; any operation whose "METHOD path" contains one
                      is skipped (e.g. "POST /v3/cohort/manifest.txt").
    SMOKE_SOFT_FAIL   If set to a truthy value, always exit 0 (still prints failures and emits
                      GitHub ::error:: annotations). Use if data-drift noise is unwanted on a tier.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


TIMEOUT = _env_int("SMOKE_TIMEOUT", 30)
MAX_BYTES = _env_int("SMOKE_MAX_BYTES", 2_000_000)
SOFT_FAIL = os.environ.get("SMOKE_SOFT_FAIL", "").lower() in {"1", "true", "yes", "on"}
SKIP = [s.strip() for s in os.environ.get("SMOKE_SKIP", "").split(",") if s.strip()]


def first_example(node: dict) -> tuple[bool, object]:
    """Return (found, value) for an OpenAPI-style example on a schema/parameter/media node."""
    if not isinstance(node, dict):
        return (False, None)
    ex = node.get("examples")
    if isinstance(ex, list) and ex:
        return (True, ex[0])
    # Parameter-level `examples` is a dict of {name: {value: ...}}; media `examples` too.
    if isinstance(ex, dict) and ex:
        first = next(iter(ex.values()))
        if isinstance(first, dict) and "value" in first:
            return (True, first["value"])
    if "example" in node:
        return (True, node["example"])
    return (False, None)


def param_example(param: dict) -> tuple[bool, object]:
    ok, val = first_example(param)
    if ok:
        return (ok, val)
    return first_example(param.get("schema") or {})


def body_example(op: dict, components: dict) -> tuple[bool, bool, object]:
    """Return (required, found, value) for a POST body example, resolving a `$ref` if present."""
    rb = op.get("requestBody")
    if not rb:
        return (False, False, None)
    required = bool(rb.get("required"))
    media = (rb.get("content") or {}).get("application/json") or {}
    ok, val = first_example(media)
    if ok:
        return (required, True, val)
    schema = media.get("schema") or {}
    ref = schema.get("$ref")
    if ref:
        target = components.get(ref.split("/")[-1]) or {}
        ok, val = first_example(target)
        return (required, ok, val)
    ok, val = first_example(schema)
    return (required, ok, val)


def build_request(path: str, op: dict, components: dict):
    """Assemble (url_path, query, body) from declared examples, or return a skip reason."""
    query: dict[str, object] = {}
    path_values: dict[str, object] = {}
    for param in op.get("parameters", []):
        loc = param.get("in")
        name = param.get("name")
        ok, val = param_example(param)
        if loc == "path":
            if not ok:
                return None, f"required path param '{name}' has no example"
            path_values[name] = val
        elif loc == "query":
            if ok:
                query[name] = val
            elif param.get("required"):
                return None, f"required query param '{name}' has no example"

    try:
        filled_path = path.format(**path_values)
    except KeyError as e:
        return None, f"unfilled path param {e}"

    _, body_found, body_val = body_example(op, components)
    body = body_val if body_found else None
    if op_needs_body(op) and not body_found:
        return None, "required body has no example"

    return (filled_path, query, body), None


def op_needs_body(op: dict) -> bool:
    rb = op.get("requestBody")
    return bool(rb and rb.get("required"))


def probe(method: str, url: str, body):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(MAX_BYTES), None
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read(MAX_BYTES), None
    except Exception as e:  # noqa: BLE001 - any transport failure is a smoke failure
        return None, "", b"", str(e)


def evaluate(status, ctype, chunk, err):
    if err is not None:
        return False, f"request failed: {err}"
    if status is None or status < 200 or status >= 300:
        return False, f"HTTP {status}"
    if "application/json" in ctype:
        raw = chunk.lstrip()
        # Error envelopes are tiny and lead the body, so a truncated read still shows them.
        if raw[:8] == b'{"error"':
            snippet = raw[:200].decode("utf-8", "replace")
            return False, f"HTTP {status} but error envelope: {snippet}"
        try:
            # Parse the raw bytes: json.loads detects the encoding per RFC 8259 and raises on
            # invalid UTF-8, so a mis-encoded payload fails rather than being silently repaired.
            # `errors="replace"` is used only for human-readable snippets below.
            parsed = json.loads(chunk)
        except Exception:
            # A parse failure is only benign when we truncated a large body at the cap; a short
            # body that won't parse is a real regression (invalid JSON under application/json).
            if len(chunk) >= MAX_BYTES:
                return True, f"HTTP {status} (json body exceeded {MAX_BYTES}-byte cap; no leading error envelope)"
            snippet = chunk[:200].decode("utf-8", "replace")
            return False, f"HTTP {status} but body is not valid JSON despite application/json: {snippet}"
        if isinstance(parsed, dict) and "error" in parsed:
            return False, f"HTTP {status} but error envelope: {json.dumps(parsed['error'])[:200]}"
    return True, f"HTTP {status}"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SMOKE_BASE_URL", "")).rstrip("/")
    if not base:
        print("error: pass a base URL as argv[1] or set SMOKE_BASE_URL", file=sys.stderr)
        return 2

    try:
        spec = json.load(urllib.request.urlopen(base + "/v3/openapi.json", timeout=TIMEOUT))
    except Exception as e:  # noqa: BLE001
        print(f"error: could not fetch {base}/v3/openapi.json: {e}", file=sys.stderr)
        return 2

    components = (spec.get("components") or {}).get("schemas") or {}
    passed, failed, skipped = [], [], []

    for path, methods in sorted(spec.get("paths", {}).items()):
        for method, op in methods.items():
            if method.lower() not in {"get", "post"}:
                continue
            tag = f"{method.upper()} {path}"
            if any(s in tag for s in SKIP):
                skipped.append((tag, "skipped via SMOKE_SKIP"))
                continue

            built, skip_reason = build_request(path, op, components)
            if built is None:
                skipped.append((tag, skip_reason))
                continue

            filled_path, query, body = built
            url = base + filled_path
            if query:
                url += "?" + urllib.parse.urlencode(query, doseq=True)

            ok, detail = evaluate(*probe(method.upper(), url, body))
            (passed if ok else failed).append((tag, detail))

    print(f"OpenAPI examples smoke test against {base}\n")
    for tag, detail in passed:
        print(f"  PASS  {tag}  ({detail})")
    for tag, detail in skipped:
        print(f"  SKIP  {tag}  ({detail})")
    for tag, detail in failed:
        print(f"  FAIL  {tag}  ({detail})")
        print(f"::error title=OpenAPI example failed::{tag} — {detail}")

    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed and not SOFT_FAIL:
        return 1
    if failed:
        print("::warning::example failures present but SMOKE_SOFT_FAIL is set; not failing the job")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
