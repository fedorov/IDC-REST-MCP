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
        "download_cohort",
    }
    assert expected <= names


async def test_tool_descriptions_are_prescriptive(server):
    tools = {t.name: t for t in await server.list_tools()}
    # get_attribute_values must steer the model to ground values before filtering.
    desc = " ".join(tools["get_attribute_values"].description.split()).lower()
    assert "before filtering" in desc


async def test_get_stats(server, parse_mcp):
    data = parse_mcp(await server.call_tool("get_stats", {}))
    assert data["series"] > 1_000_000


async def test_get_attribute_values(server, parse_mcp):
    data = parse_mcp(await server.call_tool("get_attribute_values", {"attribute": "Modality", "limit": 3}))
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


async def test_error_is_clean(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await server.call_tool("get_collection", {"collection_id": "__nope__"})
    assert "not found" in str(exc.value).lower()


async def test_resources(server):
    res = {str(r.uri) for r in await server.list_resources()}
    assert "idc://guide" in res
    guide = await server.read_resource("idc://guide")
    assert "Data model" in list(guide)[0].content
    schema = await server.read_resource("idc://schema/index")
    assert "collection_id" in list(schema)[0].content
