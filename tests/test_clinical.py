"""Clinical (non-imaging) data: the per-collection clinical tables are registered under the
``clinical`` schema, discoverable + readable via the clinical service, joinable to imaging on
``dicom_patient_id = index.PatientID`` — and kept *out* of the main ``list_tables`` catalog.

These use the shared ``ctx`` (built with IDC_API_INCLUDE_INDICES=all by default). They skip
gracefully when clinical data wasn't included, so a bundled-only/subset build stays green.
"""

from __future__ import annotations

import pytest

from idc_api.core.errors import NotFoundError


@pytest.fixture(scope="module")
def clinical_tables(ctx):
    return set(ctx.backend.list_clinical_tables())


def _skip_without_clinical(clinical_tables):
    if not clinical_tables:
        pytest.skip("clinical_index (and its data tables) not included in this build")


def test_clinical_tables_registered_but_hidden_from_list_tables(ctx, clinical_tables):
    _skip_without_clinical(clinical_tables)
    # Registered and queryable...
    assert len(clinical_tables) > 0
    # ...but NOT polluting the main catalog (list_tables stays index-focused).
    main = set(ctx.backend.list_tables())
    assert clinical_tables.isdisjoint(main)
    assert "clinical_index" in main  # the *dictionary* is a normal table, though


def test_list_clinical_tables_and_collection_filter(ctx, clinical_tables):
    _skip_without_clinical(clinical_tables)
    listing = ctx.clinical.list_clinical_tables()
    names = {t.table_name for t in listing.tables}
    # Every listed table is actually registered (queryable), and counts are populated.
    assert names <= clinical_tables
    assert all(t.column_count > 0 and t.collection_id for t in listing.tables)

    # Filtering by collection returns only that collection's tables.
    some_collection = listing.tables[0].collection_id
    filtered = ctx.clinical.list_clinical_tables(collection_id=some_collection)
    assert filtered.tables
    assert {t.collection_id for t in filtered.tables} == {some_collection}


def test_get_clinical_table_schema_has_join_key(ctx, clinical_tables):
    _skip_without_clinical(clinical_tables)
    table = sorted(clinical_tables)[0]
    sch = ctx.clinical.get_clinical_table_schema(table)
    cols = {c.name for c in sch.columns}
    # The key that links clinical rows to imaging.
    assert "dicom_patient_id" in cols


def test_get_clinical_table_returns_rows(ctx, clinical_tables):
    _skip_without_clinical(clinical_tables)
    table = sorted(clinical_tables)[0]
    res = ctx.clinical.get_clinical_table(table, max_rows=5)
    assert "dicom_patient_id" in res.columns
    assert res.row_count <= 5


def test_unknown_clinical_table_raises(ctx, clinical_tables):
    _skip_without_clinical(clinical_tables)
    with pytest.raises(NotFoundError):
        ctx.clinical.get_clinical_table_schema("definitely_not_a_clinical_table")


def test_clinical_join_to_imaging_via_sql(ctx, clinical_tables):
    # The documented idiom: join a clinical table to imaging on dicom_patient_id = PatientID.
    if "nlst_canc" not in clinical_tables:
        pytest.skip("nlst_canc not included in this build")
    res = ctx.query.run_sql(
        "SELECT count(DISTINCT i.PatientID) AS patients "
        "FROM index i "
        "JOIN clinical.nlst_canc c ON c.dicom_patient_id = i.PatientID "
        "WHERE i.collection_id = 'nlst' AND i.Modality = 'CT'"
    )
    assert res.rows[0]["patients"] > 0
