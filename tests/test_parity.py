"""Contract parity: core service, REST endpoint, and MCP tool agree for the same filter.

This is the guarantee that the two adapters stay in sync because they share one core.
"""

from __future__ import annotations

import pytest

from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

_TERMS = {"collection_id": ["rider_pilot"], "Modality": ["CT"]}


async def test_version_parity(ctx, client, parse_mcp):
    """version() is identical across core, REST, and MCP, and carries the server's own software
    version — which lines up with the string the MCP initialize handshake advertises."""
    from idc_api.mcp.server import _server_version

    core = ctx.discovery.version().model_dump(mode="json")
    rest = client.get("/v3/version").json()
    mcp_out = parse_mcp(await mcp.call_tool("get_idc_version", {}))
    assert core == rest == mcp_out
    assert core["api_version"]
    # serverInfo.version == api_version[+build]; api_version is its stable prefix.
    assert _server_version().startswith(core["api_version"])


async def test_counts_parity(ctx, client, parse_mcp):
    core_series = ctx.cohort.counts(CohortFilters(terms=_TERMS)).series

    rest_series = client.post("/v3/cohort/counts", json={"terms": _TERMS}).json()["series"]

    mcp_series = parse_mcp(await mcp.call_tool("build_cohort", {"terms": _TERMS}))["total_series"]

    assert core_series == rest_series == mcp_series > 0


def _fake_doi_get(url, headers=None, timeout=None):
    """Stub DOI content negotiation so citation tests don't touch the network."""

    class _Resp:
        status_code = 200
        text = "FAKE CITATION"

        @staticmethod
        def json():
            return {"id": "fake"}

    return _Resp()


async def test_citations_parity_and_idc_acknowledgment(ctx, client, parse_mcp, monkeypatch):
    import idc_api.core.services.citations as cite_mod

    monkeypatch.setattr(cite_mod.requests, "get", _fake_doi_get)
    terms = {"collection_id": ["rider_pilot"]}

    core = ctx.citations.get_citations(CohortFilters(terms=terms)).model_dump(mode="json")
    rest = client.post("/v3/citations", json={"filters": {"terms": terms}}).json()
    mcp_out = parse_mcp(await mcp.call_tool("get_citations", {"terms": terms}))

    # Same model serialized by both adapters (every stubbed citation is identical, so list
    # ordering can't make this spuriously fail).
    assert core == rest == mcp_out
    # The IDC paper is surfaced separately from the per-dataset citations, with guidance.
    assert core["idc_acknowledgment"] == "FAKE CITATION"
    assert core["citations"]  # per-dataset citations present, distinct from idc_acknowledgment
    assert "10.1148/rg.230180" in core["recommendation"]


async def test_clinical_parity(ctx, client, parse_mcp):
    """list_clinical_tables and get_clinical_table agree across core, REST, and MCP."""
    registered = ctx.backend.list_clinical_tables()
    if not registered:
        pytest.skip("clinical data not included in this build")

    core_list = ctx.clinical.list_clinical_tables().model_dump(mode="json")
    rest_list = client.get("/v3/clinical/tables").json()
    mcp_list = parse_mcp(await mcp.call_tool("list_clinical_tables", {}))
    assert core_list == rest_list == mcp_list

    table = sorted(registered)[0]
    core_rows = ctx.clinical.get_clinical_table(table, max_rows=5).model_dump(mode="json")
    rest_rows = client.get(f"/v3/clinical/tables/{table}/rows?max_rows=5").json()
    mcp_rows = parse_mcp(await mcp.call_tool("get_clinical_table", {"table": table, "max_rows": 5}))
    assert core_rows == rest_rows == mcp_rows
