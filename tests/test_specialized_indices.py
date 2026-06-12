"""Specialized indices (seg/sm/ct/mr/pt/…) are fetched at build time, exposed to SQL, and
joinable to the main `index` — this is what lets relational questions (e.g. "pathology slides
that have a segmentation") be answered, which the bundled-only build could not do."""

from __future__ import annotations

import pytest

from idc_api.core import schema


# --- pure unit: include resolution (offline, no build) ------------------------------------


def test_resolve_include_all_none_and_list():
    assert schema.resolve_include("all") == schema.specialized_table_names()
    assert schema.resolve_include("none") == []
    assert schema.resolve_include("") == []
    # parsed into registry order regardless of input order
    assert schema.resolve_include("seg_index, sm_index") == ["sm_index", "seg_index"]


def test_resolve_include_rejects_unknown():
    with pytest.raises(ValueError):
        schema.resolve_include("seg_index,not_a_real_index")


def test_include_token_is_stable():
    assert schema.include_token(schema.resolve_include("all")) == "all"
    assert schema.include_token([]) == "base"
    assert schema.include_token(["seg_index"]).startswith("sub-")


# --- integration: built into the DB, described, and joinable ------------------------------
# These use the shared `ctx` (built with IDC_API_INCLUDE_INDICES=all by default). They skip
# gracefully if a given index wasn't included, so a subset/offline build stays green.


@pytest.fixture(scope="module")
def tables(ctx):
    return set(ctx.backend.list_tables())


def test_specialized_tables_are_available(tables):
    for name in ("seg_index", "sm_index", "ct_index", "mr_index", "pt_index", "ann_index"):
        assert name in tables, f"{name} was not built into the DuckDB database"


def test_get_table_schema_describes_seg_index(ctx, tables):
    if "seg_index" not in tables:
        pytest.skip("seg_index not included in this build")
    cols = {c.name: c for c in ctx.query.get_table_schema("seg_index").columns}
    # the join key that links a segmentation to the image series it segments
    assert "segmented_SeriesInstanceUID" in cols
    # mode=REPEATED columns must advertise as arrays — declaring them STRING steers SQL
    # callers into `col = 'x'` / LIKE predicates the engine rejects (use list_contains)
    assert cols["SegmentedPropertyType_CodeMeanings"].type == "STRING[]"


def test_slides_with_segmentations_join(ctx, tables):
    if "seg_index" not in tables:
        pytest.skip("seg_index not included in this build")
    res = ctx.query.run_sql(
        "SELECT count(DISTINCT i.SeriesInstanceUID) AS n "
        "FROM index i "
        "JOIN seg_index seg ON seg.segmented_SeriesInstanceUID = i.SeriesInstanceUID "
        "WHERE i.Modality = 'SM'"
    )
    assert res.rows[0]["n"] > 0


def test_list_contains_filters_segmented_anatomy(ctx, tables):
    # The documented idiom for the array-typed *_CodeMeanings columns (the guide's example).
    if "seg_index" not in tables:
        pytest.skip("seg_index not included in this build")
    res = ctx.query.run_sql(
        "SELECT count(*) AS n FROM seg_index "
        "WHERE list_contains(SegmentedPropertyType_CodeMeanings, 'Liver')"
    )
    assert res.rows[0]["n"] > 0
