"""Application context: builds the backend once and wires up the services. Both adapters
obtain capabilities through a single ``AppContext`` (process-wide singleton)."""

from __future__ import annotations

from ..settings import Settings, get_settings
from .backend.duckdb_backend import DuckDBBackend
from .services import (
    CitationsService,
    CohortService,
    DiscoveryService,
    DownloadService,
    LicenseService,
    ManifestService,
    QueryService,
    ViewerService,
)


class AppContext:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.backend = DuckDBBackend(self.settings)
        self.discovery = DiscoveryService(self.backend)
        self.cohort = CohortService(self.backend, self.settings)
        self.manifest = ManifestService(self.backend, self.settings)
        self.query = QueryService(self.backend, self.settings)
        self.viewer = ViewerService(self.backend)
        self.citations = CitationsService(self.backend)
        self.licenses = LicenseService(self.backend)
        self.download = DownloadService(self.backend, self.settings)

    def close(self) -> None:
        self.backend.close()


_context: AppContext | None = None


def get_context() -> AppContext:
    global _context
    if _context is None:
        _context = AppContext()
    return _context
