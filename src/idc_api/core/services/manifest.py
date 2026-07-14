"""Manifest generation: public series URLs (AWS + GCS), s5cmd-style manifest text, and
ready-to-run ``idc`` CLI commands for a cohort. The service never moves bytes — local
download lives in ``download.py`` (MCP local mode only)."""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..filters import compile_filters
from ..models import CohortFilters, DownloadInfo

# GCS is reached via its S3-compatible interop endpoint, so URLs keep the s3:// scheme even
# for source="gcs" — only the bucket name changes, and only for two buckets (mirrors
# idc-index's _replace_aws_with_gcp_buckets exactly, so idc-index and idc download-from-manifest
# handle either source the same way; the latter only recognizes s3:// lines in a manifest file).
_AWS_TO_GCS_BUCKET = {
    "idc-open-data-two": "idc-open-idc1",
    "idc-open-data-cr": "idc-open-cr",
    # idc-open-data keeps the same bucket name on GCS (since IDC v20).
}

GCS_ENDPOINT_URL = "https://storage.googleapis.com"  # matches idc-index's gcp_endpoint_url


def remap_bucket_for_gcs(s3_url: str) -> str:
    """Remap an ``s3://bucket/path`` URL's bucket to its GCS-equivalent bucket, keeping the
    ``s3://`` scheme — the URL is meant to be read against GCS's S3-compatible endpoint
    (``GCS_ENDPOINT_URL``), not as a ``gs://`` URL."""
    rest = s3_url[len("s3://") :] if s3_url.startswith("s3://") else s3_url
    bucket, _, path = rest.partition("/")
    return f"s3://{_AWS_TO_GCS_BUCKET.get(bucket, bucket)}/{path}"


class ManifestService:
    def __init__(self, backend: QueryBackend, settings):
        self.backend = backend
        self.settings = settings

    def _series_urls(self, where: str, params: list, limit: int) -> tuple[list[str], bool]:
        """Return up to ``limit`` ``s3://`` series URLs plus a truncation flag."""
        # `where` is compile_filters output: allow-listed columns, values bound below.
        rows = self.backend.query(
            f"SELECT series_aws_url FROM index WHERE {where} "  # nosec B608
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
            urls = [remap_bucket_for_gcs(u) for u in urls]
        return urls, truncated

    def manifest_text(
        self, filters: CohortFilters, source: str = "aws", limit: int | None = None
    ) -> str:
        urls, _ = self.manifest_lines(filters, source=source, limit=limit)
        return "\n".join(urls) + ("\n" if urls else "")

    def download_info(
        self, filters: CohortFilters, total_series: int, size_TB: float
    ) -> DownloadInfo:
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
                "URLs point to public AWS S3 (and GCS) buckets; no credentials needed. Easiest: "
                "the `idc` CLI commands above (also handles either cloud). Driving it yourself: "
                "`s5cmd --no-sign-request` against these s3:// URLs for AWS; for GCS, get "
                "source=gcs URLs from get_cohort_urls/manifest.txt (still s3:// — GCS's "
                "S3-compatible endpoint) and add `--endpoint-url https://storage.googleapis.com`."
            ),
        )
