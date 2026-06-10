"""Shared fixtures. All tests run offline against the bundled idc-index Parquet."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from idc_api.core.context import get_context
from idc_api.rest.app import app


@pytest.fixture(scope="session")
def ctx():
    return get_context()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def mcp_json(result) -> object:
    """Normalize a FastMCP ``call_tool`` return into plain Python (dict/list)."""
    if isinstance(result, dict):
        # Structured-output dict; FastMCP wraps bare list/scalar returns under "result".
        return result.get("result", result)
    for block in result:  # Sequence[ContentBlock]
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    return None


@pytest.fixture
def parse_mcp():
    return mcp_json
