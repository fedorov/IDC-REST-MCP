# IDC API v3 — slim image for the REST API (and remote MCP). No MySQL/SAML/xmlsec weight.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# uv for fast, reproducible installs.
RUN pip install --no-cache-dir uv

# Install dependencies + the package (bundled idc-index-data Parquet comes along).
COPY pyproject.toml README_v3.md ./
COPY src ./src
RUN uv pip install --system .

# Bake the read-only DuckDB file at build time so cold starts are instant.
ENV IDC_API_DUCKDB_PATH=/app/idc.duckdb
RUN python -c "from idc_api.core.backend.duckdb_backend import build_database_file as b; b('/app/idc.duckdb')"

ENV PORT=8080
EXPOSE 8080

# Default: REST API. For the remote MCP server instead, override the command with:
#   idc-mcp --http --host 0.0.0.0 --port 8080
CMD ["sh", "-c", "uvicorn idc_api.rest.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
