"""Manifest generation: public series URLs (AWS + GCS), s5cmd-style manifest text, and
ready-to-run ``idc`` CLI commands for a cohort. The service never moves bytes — local
download lives in ``download.py`` (MCP local mode only)."""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..filters import compile_filters
from ..models import CohortFilters, DownloadInfo

# AWS S3 bucket -> GCS bucket remap (fixed; from idc-index _replace_aws_with_gcp_buckets).
_AWS_TO_GCS = {
    "idc-open-data-two": "idc-open-idc1",
    "idc-open-data-cr": "idc-open-cr",
    # idc-open-data keeps the same bucket name on GCS (since IDC v20).
}


def aws_url_to_gcs(aws_url: str) -> str:
    """Convert an ``s3://bucket/path`` URL to its GCS ``gs://bucket/path`` equivalent."""
    rest = aws_url[len("s3://") :] if aws_url.startswith("s3://") else aws_url
    bucket, _, path = rest.partition("/")
    return f"gs://{_AWS_TO_GCS.get(bucket, bucket)}/{path}"


class ManifestService:
    def __init__(self, backend: QueryBackend, settings):
        self.backend = backend
        self.settings = settings

    def _series_urls(self, where: str, params: list, limit: int) -> tuple[list[str], bool]:
        """Return up to ``limit`` ``s3://`` series URLs plus a truncation flag."""
        rows = self.backend.query(
            f"SELECT series_aws_url FROM index WHERE {where} "
            f"ORDER BY series_aws_url LIMIT {limit + 1}",
            params,
        ).rows
        truncated = len(rows) > limit
        return [r["series_aws_url"] for r in rows[:limit]], truncated

    def manifest_lines(
        self, filters: CohortFilters, source: str = "aws", limit: int | None = None
    ) -> tuple[list[str], bool]:
        if source not in ("aws", "gcs"):
            raise ValueError("source must be 'aws' or 'gcs'")
        limit = limit if limit is not None else self.settings.manifest_hard_cap
        where, params = compile_filters(filters)
        urls, truncated = self._series_urls(where, params, limit)
        if source == "gcs":
            urls = [aws_url_to_gcs(u) for u in urls]
        return urls, truncated

    def manifest_text(
        self, filters: CohortFilters, source: str = "aws", limit: int | None = None
    ) -> str:
        urls, _ = self.manifest_lines(filters, source=source, limit=limit)
        return "\n".join(urls) + ("\n" if urls else "")

    def download_info(self, filters: CohortFilters, total_series: int, size_TB: float) -> DownloadInfo:
        where, params = compile_filters(filters)
        preview, _ = self._series_urls(where, params, 5)
        truncated = total_series > self.settings.manifest_hard_cap

        commands: list[str] = []
        # Whole-collection selection -> the simplest idc command.
        terms = filters.terms or {}
        only_collection = (
            list(terms.keys()) == ["collection_id"]
            and not (filters.ranges or {})
            and len(terms["collection_id"]) >= 1
        )
        if only_collection:
            for cid in terms["collection_id"]:
                commands.append(f"idc download {cid} --download-dir ./idc-data")
        commands.append(
            "# Or save the full manifest (see the manifest endpoint/tool), then:\n"
            "idc download-from-manifest idc_manifest.txt --download-dir ./idc-data"
        )

        return DownloadInfo(
            total_series=total_series,
            size_TB=size_TB,
            idc_commands=commands,
            manifest_preview=preview,
            manifest_truncated=truncated,
            note=(
                "URLs point to public AWS S3 (and GCS) buckets; no credentials needed. "
                "Use s5cmd/gsutil with anonymous access, or the `idc` CLI."
            ),
        )
