"""LOCAL-ONLY download via idc-index/s5cmd.

Enabled only when ``settings.enable_local_download`` is True — i.e. the MCP server running
locally (stdio) on the user's machine. On the hosted REST API / remote MCP it raises
``UnsupportedOperationError`` (the server has no access to the caller's filesystem); callers
use the manifest/URLs instead. The heavy ``IDCClient`` is created lazily and guarded by a
lock (its DuckDB connection is not thread-safe)."""

from __future__ import annotations

import threading
from typing import Any

from ..errors import InvalidQueryError, UnsupportedOperationError


class DownloadService:
    def __init__(self, backend, settings):
        self.backend = backend
        self.settings = settings
        self._client = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.settings.enable_local_download)

    def _get_client(self):
        if self._client is None:
            from idc_index import IDCClient  # heavy import; only when actually downloading

            self._client = IDCClient()
        return self._client

    def download(
        self,
        download_dir: str,
        collection_id: list[str] | str | None = None,
        patientId: list[str] | str | None = None,
        studyInstanceUID: list[str] | str | None = None,
        seriesInstanceUID: list[str] | str | None = None,
        dry_run: bool = False,
        source_bucket_location: str = "aws",
    ) -> dict[str, Any]:
        if not self.available():
            raise UnsupportedOperationError(
                "Local download is disabled in this deployment. Use the manifest/URLs and "
                "download with the `idc` CLI or s5cmd. (Local download is available when the "
                "IDC MCP server runs locally on your machine.)"
            )
        if not any([collection_id, patientId, studyInstanceUID, seriesInstanceUID]):
            raise InvalidQueryError(
                "Provide at least one selection: collection_id, patientId, "
                "studyInstanceUID, or seriesInstanceUID."
            )

        with self._lock:
            client = self._get_client()
            client.download_from_selection(
                downloadDir=download_dir,
                collection_id=collection_id,
                patientId=patientId,
                studyInstanceUID=studyInstanceUID,
                seriesInstanceUID=seriesInstanceUID,
                dry_run=dry_run,
                source_bucket_location=source_bucket_location,
            )
        return {
            "status": "dry_run" if dry_run else "completed",
            "download_dir": download_dir,
            "source": source_bucket_location,
        }
