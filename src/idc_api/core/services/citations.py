"""Citation generation from a cohort's source DOIs (mirrors idc-index ``citations_from_selection``).

Resolves distinct ``source_DOI`` values for the selection plus the main IDC publication
(10.1148/rg.230180), then fetches formatted citations via DOI content negotiation.
"""

from __future__ import annotations

import requests

from ..backend.base import QueryBackend
from ..errors import InvalidQueryError
from ..filters import compile_filters
from ..models import CitationsResult, CohortFilters

# Short name -> DOI content-negotiation MIME type (see https://citation.crosscite.org).
CITATION_FORMATS = {
    "apa": "text/x-bibliography; style=apa; locale=en-US",
    "bibtex": "application/x-bibtex",
    "csl-json": "application/vnd.citationstyles.csl+json",
    "turtle": "text/turtle",
}

_MAIN_IDC_DOI = "10.1148/rg.230180"


class CitationsService:
    def __init__(self, backend: QueryBackend):
        self.backend = backend

    def get_citations(
        self, filters: CohortFilters, citation_format: str = "apa", timeout: float = 30.0
    ) -> CitationsResult:
        fmt = citation_format.lower()
        if fmt not in CITATION_FORMATS:
            raise InvalidQueryError(
                f"Unknown citation_format {citation_format!r}. "
                f"Choose one of: {', '.join(CITATION_FORMATS)}."
            )
        accept = CITATION_FORMATS[fmt]

        where, params = compile_filters(filters)
        dois = [
            r["source_DOI"]
            for r in self.backend.query(
                f"SELECT DISTINCT source_DOI FROM index WHERE {where} "
                f"AND source_DOI IS NOT NULL AND source_DOI <> ''",
                params,
            ).rows
        ]
        dois.append(_MAIN_IDC_DOI)

        citations: list = []
        for doi in dois:
            try:
                resp = requests.get(
                    f"https://dx.doi.org/{doi}", headers={"accept": accept}, timeout=timeout
                )
            except requests.RequestException:
                continue
            if resp.status_code == 200:
                citations.append(resp.json() if fmt == "csl-json" else resp.text.strip())

        return CitationsResult(format=fmt, citations=citations)
