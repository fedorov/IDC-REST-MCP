"""Contract parity: core service, REST endpoint, and MCP tool agree for the same filter.

This is the guarantee that the two adapters stay in sync because they share one core.
"""

from __future__ import annotations

from idc_api.core.models import CohortFilters
from idc_api.mcp.server import mcp

_TERMS = {"collection_id": ["rider_pilot"], "Modality": ["CT"]}


async def test_counts_parity(ctx, client, parse_mcp):
    core_series = ctx.cohort.counts(CohortFilters(terms=_TERMS)).series

    rest_series = client.post("/v3/cohort/counts", json={"terms": _TERMS}).json()["series"]

    mcp_series = parse_mcp(await mcp.call_tool("build_cohort", {"terms": _TERMS}))["total_series"]

    assert core_series == rest_series == mcp_series > 0
