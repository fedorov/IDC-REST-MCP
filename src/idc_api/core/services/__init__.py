"""Core services — backend-agnostic domain logic returning Pydantic models.

Each service is a thin, stateless wrapper around a ``QueryBackend``; the REST and MCP
adapters call these and never touch SQL or the backend directly.
"""

from .citations import CitationsService
from .clinical import ClinicalService
from .cohort import CohortService
from .discovery import DiscoveryService
from .licenses import LicenseService
from .manifest import ManifestService
from .query import QueryService
from .viewer import ViewerService

__all__ = [
    "CitationsService",
    "ClinicalService",
    "CohortService",
    "DiscoveryService",
    "LicenseService",
    "ManifestService",
    "QueryService",
    "ViewerService",
]
