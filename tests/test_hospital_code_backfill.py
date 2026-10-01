import pytest

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.hospital_code_backfill import (
    build_inventory,
    upsert_inventory,
)


pytestmark = pytest.mark.django_db

EPIC = 'urn:oid:1.2.840.114350.1.13.670.2.7.5.737384.352'
CERNER = 'https://fhir.cerner.com/11e960ca-465e-403d-a8ac-dfa9be65dd83/codeSet/72'


def primary(system, code, label, count, **values):
    return {
        'resource_type': 'Observation', 'field_path': 'code',
        'coding_system': system, 'coding_code': code,
        'source_label': label, 'n_records': count,
        'category_top': 'laboratory', **values,
    }


def test_build_inventory_unions_only_supplement_keys_and_preserves_exact_systems():
    result = build_inventory([
        primary(EPIC, 'ALB', 'albumin', 8),
        primary(EPIC, 'ALB', 'Albumin serum', 2, field_path='component[].code'),
        primary(CERNER, '3595840059', 'education needs', 4,
                category_top='survey'),
        primary('http://example.test/local', 'UNSAFE', 'unknown vendor', 99),
    ], unit_rows=[
        {'coding_system': EPIC, 'coding_code': 'ALB', 'unit_display': 'g/dL',
         'unit_ucum': 'g/dL', 'n_records': 7},
        {'coding_system': EPIC, 'coding_code': 'ALB', 'unit_display': '<no unit>',
         'unit_ucum': '', 'n_records': 1},
    ], supplemental_rows=[
        # The CSV repeats a code count for every unit. An overlapping primary
        # key is ignored, and the supplement-only key takes max rather than sum.
        {'coding_system': EPIC, 'coding_code': 'ALB', 'source_label': 'old albumin',
         'fhir_n_records': '500'},
        {'coding_system': EPIC, 'coding_code': 'OLD', 'source_label': 'legacy test',
         'fhir_n_records': '3'},
        {'coding_system': EPIC, 'coding_code': 'OLD', 'source_label': 'legacy test',
         'fhir_n_records': '3'},
    ])

    assert set(result.rows) == {(EPIC, 'ALB'), (EPIC, 'OLD'), (CERNER, '3595840059')}
    albumin = result.rows[(EPIC, 'ALB')]
    assert albumin.occurrence_count == 10
    assert albumin.source_code_description == 'albumin'
    assert albumin.domain_id == 'Measurement'
    assert albumin.source_unit_evidence == [{
        'display': 'g/dL', 'code': 'g/dL', 'count': 7,
        'source': 'healthtree-unmapped-v2',
    }]
    assert result.rows[(EPIC, 'OLD')].occurrence_count == 3
    assert result.rows[(EPIC, 'OLD')].domain_id == ''
    assert result.rows[(CERNER, '3595840059')].domain_id == 'Observation'
    assert result.stats['skipped_non_hospital'] == 1
    assert result.stats['primary_keys'] == 2
    assert result.stats['supplement_keys'] == 1


def test_coded_observation_values_are_not_misclassified_as_measurements():
    result = build_inventory([
        primary(EPIC, 'ANSWER', 'yes', 5,
                field_path='valueCodeableConcept', category_top='laboratory'),
    ])

    row = result.rows[(EPIC, 'ANSWER')]
    assert (row.domain_id, row.omop_table) == ('Observation', 'observation')


def test_upsert_is_idempotent_and_does_not_overwrite_curator_decisions():
    curated = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='ALB',
        source_code_description='Curator label', occurrence_count=2,
        status='rejected', origin='curator', domain_id='', omop_table='',
    )
    build = build_inventory([
        primary(EPIC, 'ALB', 'extract label', 10),
        primary(CERNER, 'NEW', 'new code', 4, category_top='survey'),
    ], unit_rows=[
        {'coding_system': EPIC, 'coding_code': 'ALB', 'unit_display': 'g/dL',
         'unit_ucum': 'g/dL', 'n_records': 8},
    ])

    first = upsert_inventory(build, provenance='healthtree-unmapped-v2')
    second = upsert_inventory(build, provenance='healthtree-unmapped-v2')

    assert first == {
        'total': 2, 'new': 1, 'existing': 1, 'updated': 1, 'dry_run': False,
    }
    assert second == {
        'total': 2, 'new': 0, 'existing': 2, 'updated': 0, 'dry_run': False,
    }
    curated.refresh_from_db()
    assert curated.source_code_description == 'Curator label'
    assert curated.status == 'rejected'
    assert curated.domain_id == ''
    assert curated.occurrence_count == 10
    assert curated.source_unit_evidence[0]['code'] == 'g/dL'
    imported = SourceCodeConceptMapping.objects.get(
        source_vocabulary_id=CERNER, source_code='NEW',
    )
    assert imported.origin_system == 'healthtree-unmapped-v2'
    assert imported.domain_id == 'Observation'


def test_dry_run_does_not_write():
    build = build_inventory([primary(EPIC, 'ALB', 'albumin', 8)])

    outcome = upsert_inventory(
        build, provenance='healthtree-unmapped-v2', dry_run=True,
    )

    assert outcome['new'] == 1
    assert outcome['dry_run'] is True
    assert not SourceCodeConceptMapping.objects.exists()
