"""Runtime configuration for the IDC API (env-driven, prefix ``IDC_API_``)."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IDC_API_", env_file=".env", extra="ignore")

    # --- DuckDB engine ---
    # If set, open this prebuilt DuckDB file read-only (e.g. baked into the image). If None,
    # the backend builds one from the bundled idc-index Parquet into a temp cache path.
    duckdb_path: str | None = None
    duckdb_memory_limit: str = "4GB"
    duckdb_threads: int = 4
    duckdb_temp_directory_size: str = "4GB"

    # Specialized idc-index tables (seg/ann/sm/ct/mr/pt, clinical, …) to build into the DuckDB
    # database so run_sql can query/join them. ``"all"`` (default) fetches every specialized
    # index from idc-index releases at build time (needs network on first build); ``"none"``
    # keeps only the bundled tables (fully offline); or a comma-separated allow-list of names.
    # Ignored when ``duckdb_path`` points at a prebuilt file (whatever was baked is used as-is).
    include_indices: str = "all"

    # --- Guarded SQL tool ---
    sql_max_rows: int = 5000  # default row cap when a caller does not specify max_rows
    # Hard ceiling on run_sql max_rows: a caller-supplied max_rows is silently clamped to this
    # so a single query can never dump an unbounded result into the caller's (LLM) context. The
    # `truncated` flag still signals that the result was capped. Raise only if a deployment needs
    # genuinely larger raw exports (memory_limit / timeout remain the other backstops).
    sql_max_rows_cap: int = 10000
    sql_timeout_seconds: float = 30.0

    # --- Cohort / manifest ---
    default_page_size: int = 100
    max_page_size: int = 5000
    manifest_hard_cap: int = 100_000  # max series rows a single manifest may enumerate

    # --- Build / version ---
    # Optional deploy-time build stamp (e.g. a short git SHA) appended to the package version
    # advertised in the MCP initialize handshake (serverInfo.version). The package version alone
    # is static across redeploys of the same release (e.g. 3.0.0.dev0), so set this at deploy
    # time — IDC_API_BUILD=$(git rev-parse --short HEAD) — to get a version string that moves on
    # every redeploy, making it possible to confirm which build a hosted instance is running.
    build: str | None = None

    # --- Deployment mode ---
    # True only when the MCP server runs locally (stdio) on the user's machine, where it may
    # actually download files. Hosted REST / remote MCP keep this False (manifests only).
    enable_local_download: bool = False

    # --- MCP HTTP transport security ---
    # The MCP streamable-HTTP transport has DNS-rebinding protection that allow-lists the Host
    # header (localhost-only by default), which rejects a hosted domain (e.g. Cloud Run) with
    # HTTP 421. This service is public, unauthenticated, and read-only, so that protection adds
    # nothing — default it off. To re-enable, set mcp_dns_rebinding_protection=true and list the
    # serving host(s)/origin(s). Affects only the HTTP transport; stdio is unaffected.
    mcp_dns_rebinding_protection: bool = False
    mcp_allowed_hosts: list[str] = []
    mcp_allowed_origins: list[str] = []

    # --- REST ---
    cors_allow_origins: list[str] = ["*"]
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Audit logging ---
    # How much of a caller's SQL (run_sql tool / POST /v3/sql) lands in the structured audit
    # log: "snippet" logs the first sql_log_chars characters -- readable, useful for diagnosing
    # a slow/abusive query; "hash" logs a short digest instead -- lets you correlate repeated
    # identical queries across log lines without putting query text in logs at all. IDC data is
    # public with no auth, so this is about log-line hygiene, not confidentiality.
    sql_log_mode: Literal["snippet", "hash"] = "snippet"
    sql_log_chars: int = 200


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
