# IDC API — slim image for the REST API (and remote MCP).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# uv for fast, reproducible installs.
RUN pip install --no-cache-dir uv

# Install dependencies + the package (bundled idc-index-data Parquet comes along).
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system .

# Bake the read-only DuckDB file at build time so cold starts are instant. With no argument,
# build_database_file includes ALL specialized indices (seg/ann/sm/ct/mr/pt, clinical, …),
# fetched from idc-index-data releases — so `docker build` needs network here (~40 MB). The
# baked file is used as-is at runtime (IDC_API_DUCKDB_PATH set below), so the container itself
# stays offline. To bake a smaller image, pass a subset, e.g. b('/app/idc.duckdb', ['seg_index']).
ENV IDC_API_DUCKDB_PATH=/app/idc.duckdb
RUN python -c "from idc_api.core.backend.duckdb_backend import build_database_file as b; b('/app/idc.duckdb')"

# Drop root before serving: the app only ever reads /app/idc.duckdb, so a non-root user with no
# write access anywhere but its own home is enough and limits what a code-execution bug could do.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 appuser \
    && chown appuser:appuser /app/idc.duckdb
USER appuser

ENV PORT=8080
EXPOSE 8080

# Default: REST API. For the remote MCP server instead, override the command with:
#   idc-mcp --http --host 0.0.0.0 --port 8080
CMD ["sh", "-c", "uvicorn idc_api.rest.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
