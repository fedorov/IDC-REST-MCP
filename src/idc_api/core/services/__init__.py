"""Core services — backend-agnostic domain logic returning Pydantic models.

Each service is a thin, stateless wrapper around a ``QueryBackend``; the REST and MCP
adapters call these and never touch SQL or the backend directly.
"""

from .citations import CitationsService
from .cohort import CohortService
from .discovery import DiscoveryService
from .download import DownloadService
from .licenses import LicenseService
from .manifest import ManifestService
from .query import QueryService
from .viewer import ViewerService

__all__ = [
    "CitationsService",
    "CohortService",
    "DiscoveryService",
    "DownloadService",
    "LicenseService",
    "ManifestService",
    "QueryService",
    "ViewerService",
]
