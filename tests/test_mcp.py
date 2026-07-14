"""MCP adapter: tools registered, calls return correct data, errors are clean, resources read."""

from __future__ import annotations

import pytest

from idc_api.mcp.server import mcp


@pytest.fixture(scope="module")
def server():
    return mcp


async def test_tools_registered(server):
    names = {t.name for t in await server.list_tools()}
    expected = {
        "get_idc_version",
        "get_stats",
        "list_collections",
        "get_collection",
        "list_attributes",
        "get_attribute_values",
        "list_tables",
        "get_table_schema",
        "build_cohort",
        "run_sql",
        "get_cohort_urls",
        "get_viewer_url",
        "get_citations",
        "get_licenses",
    }
    assert expected <= names
    # Removed in beta: retrieval is manifests/URLs only, so no local-download tool.
    assert "download_cohort" not in names


async def test_tool_descriptions_are_prescriptive(server):
    tools = {t.name: t for t in await server.list_tools()}
    # get_attribute_values must steer the model to ground values before filtering.
    desc = " ".join(tools["get_attribute_values"].description.split()).lower()
    assert "before filtering" in desc


async def test_get_stats(server, parse_mcp):
    data = parse_mcp(await server.call_tool("get_stats", {}))
    assert data["series"] > 1_000_000


async def test_get_attribute_values(server, parse_mcp):
    data = parse_mcp(
        await server.call_tool("get_attribute_values", {"attribute": "Modality", "limit": 3})
    )
    assert data["attribute"] == "Modality" and data["values"]


async def test_build_cohort(server, parse_mcp):
    data = parse_mcp(
        await server.call_tool("build_cohort", {"terms": {"collection_id": ["rider_pilot"]}})
    )
    assert data["total_series"] > 0
    assert any("rider_pilot" in c for c in data["download"]["idc_commands"])


async def test_run_sql(server, parse_mcp):
    data = parse_mcp(await server.call_tool("run_sql", {"sql": "SELECT 1 AS a"}))
    assert data["rows"] == [{"a": 1}]


async def test_run_sql_error_carries_engine_hint(server):
    # A SQL mistake must return DuckDB's self-correction hints (candidate columns,
    # "Did you mean") to the agent — not the guard's generic "Internal error" fallback.
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await server.call_tool("run_sql", {"sql": "SELECT no_such_column FROM index"})
    msg = str(exc.value)
    assert "no_such_column" in msg
    assert "Internal error" not in msg


async def test_error_is_clean(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await server.call_tool("get_collection", {"collection_id": "__nope__"})
    assert "not found" in str(exc.value).lower()


def test_server_version_is_our_build_not_sdk_fallback(server):
    """initialize must advertise our package version, not the MCP SDK's own version.

    The low-level server defaults version to None, which makes the handshake echo the `mcp`
    SDK version — useless for tracking this server. We set it explicitly; guard that wiring
    (and the private _mcp_server reach-in it depends on) against SDK changes.
    """
    from importlib.metadata import version

    from idc_api.mcp.server import _server_version

    advertised = server._mcp_server.version
    assert advertised, "serverInfo.version unset → SDK-version fallback"
    assert advertised == _server_version()
    assert advertised.startswith(version("idc-api"))
    assert advertised != version("mcp")
    # the value the initialize handshake actually returns
    opts = server._mcp_server.create_initialization_options()
    assert opts.server_version == advertised


def test_server_version_appends_build_stamp(monkeypatch):
    """A deploy-time IDC_API_BUILD stamp is appended so the version moves on every redeploy."""
    import idc_api.settings as settings_mod
    from idc_api.mcp.server import _server_version

    monkeypatch.setenv("IDC_API_BUILD", "deadbee")
    settings_mod._settings = None  # drop cache so the stamped env is read
    try:
        assert _server_version().endswith("+deadbee")
    finally:
        settings_mod._settings = None  # don't leak the stamped settings to other tests


async def test_resources(server):
    res = {str(r.uri) for r in await server.list_resources()}
    assert "idc://guide" in res
    guide = await server.read_resource("idc://guide")
    assert "Data model" in list(guide)[0].content
    schema = await server.read_resource("idc://schema/index")
    assert "collection_id" in list(schema)[0].content


def test_http_app_serves_both_slash_forms_without_redirect():
    """`/mcp` and `/mcp/` both answer directly. FastMCP alone 307s the trailing-slash form to
    the bare one, which forces clients and proxies to replay the POST body."""
    from fastapi.testclient import TestClient

    from idc_api.mcp.server import http_app
    from idc_api.settings import get_settings

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(http_app()) as c:
        for path in ("/mcp", "/mcp/"):
            r = c.post(path, json=body, headers=headers, follow_redirects=False)
            assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"
            assert r.json()["result"]["serverInfo"]["name"] == "IDC (Imaging Data Commons)"
            # NCI policy: HSTS on every response of the hosted transport, same as REST. The
            # expected max-age comes from settings, which the environment may override —
            # 0 is documented as "disabled", in which case the header must be absent.
            max_age = get_settings().hsts_max_age
            expected = f"max-age={max_age}; includeSubDomains" if max_age else None
            assert r.headers.get("strict-transport-security") == expected


@pytest.mark.parametrize("configured", ["/mcp", "/mcp/"])
def test_http_app_is_slash_agnostic_in_configured_path(configured):
    """Both spellings are routed whichever one FastMCP was configured with. Only reachable by
    editing the FastMCP(...) call — FASTMCP_STREAMABLE_HTTP_PATH cannot reach it, since
    FastMCP.__init__ always passes streamable_http_path= explicitly and that outranks env."""
    from mcp.server.fastmcp import FastMCP
    from starlette.routing import Route

    from idc_api.mcp.server import http_app

    app = http_app(FastMCP("probe", streamable_http_path=configured))
    paths = {r.path for r in app.router.routes if isinstance(r, Route)}
    assert {"/mcp", "/mcp/"} <= paths
    assert app.router.redirect_slashes is False
