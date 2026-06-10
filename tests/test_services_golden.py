"""Golden tests: v3 results must match idc-index (IDCClient) reading the same Parquet."""

from __future__ import annotations

import pytest

from idc_api.core.models import CohortFilters


@pytest.fixture(scope="module")
def idc():
    IDCClient = pytest.importorskip("idc_index").IDCClient
    return IDCClient()


def test_stats_series_matches_idc_index(ctx, idc):
    ours = ctx.discovery.stats().series
    theirs = int(
        idc.sql_query("SELECT count(DISTINCT SeriesInstanceUID) n FROM index")["n"].iloc[0]
    )
    assert ours == theirs


def test_cohort_counts_match_idc_index(ctx, idc):
    ours = ctx.cohort.counts(CohortFilters(terms={"Modality": ["MR"], "BodyPartExamined": ["BREAST"]}))
    df = idc.sql_query(
        "SELECT count(DISTINCT PatientID) p, count(DISTINCT StudyInstanceUID) st, "
        "count(DISTINCT SeriesInstanceUID) se FROM index "
        "WHERE Modality='MR' AND BodyPartExamined='BREAST'"
    )
    assert ours.patients == int(df["p"].iloc[0])
    assert ours.studies == int(df["st"].iloc[0])
    assert ours.series == int(df["se"].iloc[0])


def test_viewer_url_matches_idc_index(ctx, idc):
    suid = ctx.query.run_sql(
        "SELECT SeriesInstanceUID FROM index WHERE collection_id='rider_pilot' "
        "AND Modality='CT' LIMIT 1"
    ).rows[0]["SeriesInstanceUID"]
    ours = ctx.viewer.get_viewer_url(series_instance_uid=suid).viewer_url
    theirs = idc.get_viewer_URL(seriesInstanceUID=suid)
    assert ours == theirs
