"""REST adapter: endpoint shapes, error mapping, and the SQL guard over HTTP."""

from __future__ import annotations


def test_version_and_stats(client):
    from idc_api.core.version import server_version

    v = client.get("/v3/version").json()
    assert v["idc_version"].startswith("v")
    # The server's own software version, distinct from the IDC data version, and consistent with
    # the shared helper the MCP handshake / OpenAPI also use.
    assert v["api_version"] and server_version().startswith(v["api_version"])
    assert "build" in v
    s = client.get("/v3/stats").json()
    assert s["series"] > 1_000_000 and s["collections"] > 100


def test_root_reports_server_version(client):
    from idc_api.core.version import server_version

    root = client.get("/v3").json()
    assert root["server_version"] == server_version()


def test_all_routes_live_under_v3(client):
    # Every versioned route lives under /v3; the only thing outside it is the root redirect.
    assert client.get("/v3/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 404


def test_root_redirects_to_docs(client):
    r = client.get("/", follow_redirects=False)
    # 307, not 301: the target is version-numbered and a permanent redirect would outlive /v3.
    assert r.status_code == 307
    assert r.headers["location"] == "/v3/docs"
    assert client.get("/").status_code == 200  # and the target actually resolves


def test_root_redirect_is_not_a_catch_all(client):
    # Only `/` redirects. Every other unmatched path must still 404 — an MCP client probing
    # /.well-known/… during auth discovery would misread a 200 HTML page as auth metadata.
    for path in ("/zzz", "/.well-known/oauth-protected-resource", "/mcp"):
        assert client.get(path, follow_redirects=False).status_code == 404, path


def test_collections_and_detail(client):
    cols = client.get("/v3/collections").json()
    assert any(c["collection_id"] == "rider_pilot" for c in cols)
    detail = client.get("/v3/collections/rider_pilot").json()
    assert detail["series"] > 0 and detail["modalities"]


def test_unknown_collection_404(client):
    r = client.get("/v3/collections/__nope__")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_attribute_values(client):
    r = client.get("/v3/attributes/Modality/values?limit=3").json()
    assert r["attribute"] == "Modality"
    assert {"CT", "MR"} & {v["value"] for v in r["values"]} or r["values"]


def test_cohort_manifest(client):
    body = {"filters": {"terms": {"collection_id": ["rider_pilot"]}}, "page_size": 3}
    m = client.post("/v3/cohort/manifest", json=body).json()
    assert m["total_series"] > 0
    assert m["returned"] <= 3
    assert any("rider_pilot" in cmd for cmd in m["download"]["idc_commands"])


def test_manifest_text_gcs(client):
    body = {"filters": {"terms": {"collection_id": ["rider_pilot"]}}, "source": "gcs", "limit": 2}
    text = client.post("/v3/cohort/manifest.txt", json=body).text.strip()
    assert text.splitlines()[0].startswith("gs://")


def test_sql_ok_and_guarded(client):
    ok = client.post("/v3/sql", json={"sql": "SELECT 1 AS a"}).json()
    assert ok["rows"] == [{"a": 1}]
    bad = client.post("/v3/sql", json={"sql": "DROP TABLE index"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_query"


def test_sql_engine_error_is_a_clean_400(client):
    # DuckDB binder errors map to invalid_query with the engine's hint in the message,
    # mirroring the MCP adapter (same InvalidQueryError from the shared backend).
    r = client.post("/v3/sql", json={"sql": "SELECT no_such_column FROM index"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_query"
    assert "no_such_column" in err["message"]


def test_download_disabled_returns_501(client):
    r = client.post(
        "/v3/download", json={"download_dir": "/tmp/x", "collection_id": ["rider_pilot"]}
    )
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "unsupported_operation"


def test_openapi_served(client):
    from idc_api.core.version import server_version

    spec = client.get("/v3/openapi.json").json()
    assert spec["info"]["title"] == "IDC API"
    # info.version is driven by the package/build version, not a hardcoded literal.
    assert spec["info"]["version"] == server_version()
    assert "/v3/cohort/manifest" in spec["paths"]
