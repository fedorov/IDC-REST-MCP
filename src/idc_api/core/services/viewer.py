"""IDC viewer URL construction (OHIF for radiology, SLIM for slide microscopy).

URL templates mirror idc-index ``get_viewer_URL`` exactly.
"""

from __future__ import annotations

from ..backend.base import QueryBackend
from ..errors import InvalidQueryError, NotFoundError
from ..models import ViewerURL

_VALID_VIEWERS = ("ohif_v2", "ohif_v3", "slim")
_BASE = "https://viewer.imaging.datacommons.cancer.gov"


class ViewerService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def get_viewer_url(
        self,
        series_instance_uid: str | None = None,
        study_instance_uid: str | None = None,
        viewer: str | None = None,
    ) -> ViewerURL:
        if not series_instance_uid and not study_instance_uid:
            raise InvalidQueryError(
                "Provide series_instance_uid or study_instance_uid (or both)."
            )
        if viewer is not None and viewer not in _VALID_VIEWERS:
            raise InvalidQueryError(f"viewer must be one of {_VALID_VIEWERS}.")

        # Resolve the study (and the modalities in it, to pick a default viewer).
        if study_instance_uid is None:
            rows = self.backend.query(
                "SELECT DISTINCT StudyInstanceUID FROM index WHERE SeriesInstanceUID = ?",
                [series_instance_uid],
            ).rows
            if not rows:
                raise NotFoundError(
                    f"SeriesInstanceUID not found in IDC: {series_instance_uid!r}"
                )
            study_instance_uid = rows[0]["StudyInstanceUID"]

        modalities = [
            r["Modality"]
            for r in self.backend.query(
                "SELECT DISTINCT Modality FROM index WHERE StudyInstanceUID = ?",
                [study_instance_uid],
            ).rows
        ]
        if not modalities:
            raise NotFoundError(f"StudyInstanceUID not found in IDC: {study_instance_uid!r}")

        if viewer is None:
            viewer = "slim" if "SM" in modalities else "ohif_v3"

        url = self._build_url(viewer, study_instance_uid, series_instance_uid)
        return ViewerURL(
            viewer_url=url,
            viewer=viewer,
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
        )

    @staticmethod
    def _build_url(viewer: str, study: str, series: str | None) -> str:
        if viewer == "ohif_v2":
            base = f"{_BASE}/viewer/{study}"
            return base if not series else f"{base}?SeriesInstanceUID={series}"
        if viewer == "ohif_v3":
            base = f"{_BASE}/v3/viewer/?StudyInstanceUIDs={study}"
            return base if not series else f"{base}&initialSeriesInstanceUID={series}"
        # slim
        base = f"{_BASE}/slim/studies/{study}"
        return base if not series else f"{base}/series/{series}"
