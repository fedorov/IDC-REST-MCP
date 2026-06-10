"""Runtime configuration for IDC API v3 (env-driven, prefix ``IDC_API_``)."""

from __future__ import annotations

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

    # --- Guarded SQL tool ---
    sql_max_rows: int = 5000
    sql_timeout_seconds: float = 30.0

    # --- Cohort / manifest ---
    default_page_size: int = 100
    max_page_size: int = 5000
    manifest_hard_cap: int = 100_000  # max series rows a single manifest may enumerate

    # --- Deployment mode ---
    # True only when the MCP server runs locally (stdio) on the user's machine, where it may
    # actually download files. Hosted REST / remote MCP keep this False (manifests only).
    enable_local_download: bool = False

    # --- REST ---
    cors_allow_origins: list[str] = ["*"]
    host: str = "127.0.0.1"
    port: int = 8000


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
