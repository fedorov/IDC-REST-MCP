"""Implementation (software) version of this server — the ``idc-api`` package/build version,
distinct from the IDC *data* version (``idc_version`` / ``idc_index_data_version``).

One source of truth for "which build is running": the REST ``/v3/version`` endpoint and root,
the FastAPI OpenAPI ``info.version``, and the MCP ``initialize`` handshake (``serverInfo.version``)
all resolve it here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from ..settings import get_settings

_DIST_NAME = "idc-api"


def package_version() -> str:
    """The installed ``idc-api`` distribution version (e.g. ``3.0.0.dev0``); a clear sentinel
    when running from a source tree with no install."""
    try:
        return _pkg_version(_DIST_NAME)
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+unknown"


def build_stamp() -> str | None:
    """Optional deploy-time build stamp (``IDC_API_BUILD``, e.g. a short git SHA), or ``None``
    when unset/empty."""
    return get_settings().build or None


def server_version() -> str:
    """The full software version advertised to callers: the package version with the build stamp
    appended as a PEP 440 local segment when set (and not already present), so the string moves on
    every redeploy of the same release."""
    base = package_version()
    build = build_stamp()
    return f"{base}+{build}" if build and "+" not in base else base
