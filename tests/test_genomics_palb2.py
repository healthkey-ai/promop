"""Reviewed naming must not erase original evidence or reviewer decisions."""
from importlib import import_module
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from rest_framework.exceptions import ValidationError

from omop_core.models import FieldConceptMapping, Measurement, Observation, PatientRecord
from omop_core.services.genomics import list_variants, save_variant, replace_priority_fields
from omop_core.services.genomics_catalog import catalog, marker_for_variant
from omop_core.services.patient_record_service import refresh_patient_record
from tests.test_genomics_catalog import client_for
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0233_genomics_palb2_naming')


def test_catalog_versions_reviewed_naming_without_editing_frozen_input():
    frozen = json.loads((Path(__file__).parents[1] / 'omop_core/data/genomics_catalog_v1.json').read_text())
    assert frozen['version'] == 1
    assert any(m['key'] == 'palb1' for m in frozen['markers'])
    assert catalog()['version'] == 2
    marker = marker_for_variant({'gene': 'PALB1'})
    assert marker['key'] == 'palb2'
    assert marker['field_name'] == 'genomics_palb2'
    assert marker_for_variant({'gene': 'PALB2', 'marker_key': 'palb1'}) == marker
    assert marker_for_variant({'gene': 'PALB10'}) is None


def legacy_finding(person, source='genomics:palb1'):
    saved = save_variant(person, {'gene': 'PALB2', 'variant': 'Unchanged PALB1 report text',
                                  'test_date': '2026-07-01'})
    Measurement.objects.filter(pk=saved['id']).update(
        measurement_source_value=source, qualifier_source_value='PALB1')
    gene_mapping = FieldConceptMapping.objects.get(field_name='genetic_mutations.gene')
    model = Measurement if gene_mapping.omop_table == 'measurement' else Observation
    prefix = gene_mapping.omop_table
    model.objects.filter(**{prefix + '_event_id': saved['id'],
                           prefix + '_source_value': gene_mapping.source_value}).update(value_as_string='PALB1')
    refresh_patient_record(person)
    return saved['id']


@pytest.mark.parametrize('source', ['genomics:palb1', '81252-9', 'reviewed:palb'])
def test_old_facts_project_corrected_gene_without_changing_source_or_unchanged_echo(setup, source):
    person, record, staff = setup
    if source == 'reviewed:palb':
        FieldConceptMapping.objects.filter(field_name='genomics_palb2').update(source_value=source)
    pk = legacy_finding(person, source)
    before = list(Measurement.objects.filter(person=person).values())
    components = list(Observation.objects.filter(person=person).values())
    rows = list_variants(person)
    assert rows[0]['gene'] == 'PALB2'
    assert rows[0]['source_gene'] == 'PALB1'
    assert rows[0]['variant'] == 'Unchanged PALB1 report text'
    record.refresh_from_db()
    assert record.genomics_palb2 == rows
    response = client_for(staff).patch(f'/api/v1/patient-records/{person.pk}/',
        {'genomics_palb1': rows}, format='json')
    assert response.status_code == 200, response.data
    replace_priority_fields(person, {'genomics_palb2': rows})
    assert list(Measurement.objects.filter(person=person).values()) == before
    assert list(Observation.objects.filter(person=person).values()) == components
    edited = save_variant(person, {**rows[0], 'interpretation': 'VUS'}, pk)
    assert edited['gene'].upper() == 'PALB2'
    assert model_has_original_gene(person)


def model_has_original_gene(person):
    return (Measurement.objects.filter(person=person, value_as_string='PALB1', is_erroneous=True).exists()
            or Observation.objects.filter(person=person, value_as_string='PALB1', is_erroneous=True).exists())


def test_legacy_field_and_marker_inputs_write_canonical_findings_and_reject_conflicts(setup):
    person, record, staff = setup
    client = client_for(staff)
    url = f'/api/v1/patient-records/{person.pk}/'
    payload = [{'gene': 'PALB1', 'marker_key': 'palb1', 'variant': 'example'}]
    response = client.patch(url, {'genomics_palb1': payload}, format='json')
    assert response.status_code == 200, response.data
    assert 'genomics_palb1' not in response.data
    row = response.data['genomics_palb2'][0]
    assert row['gene'].upper() == 'PALB2'
    assert row['marker_key'] == 'palb2'
    assert Measurement.objects.get(pk=row['id']).qualifier_source_value == 'PALB2'
    assert client.patch(url, {'genomics_palb1': [], 'genomics_palb2': [row]}, format='json').status_code == 400
    assert len(list_variants(person)) == 1
    assert client.patch(url, {'genomics_palb1': []}, format='json').status_code == 200
    assert list_variants(person) == []


def test_source_gene_is_server_owned_and_rejected_mapping_blocks_legacy_alias(setup):
    person, _, staff = setup
    with pytest.raises(ValidationError, match='Read-only'):
        save_variant(person, {'gene': 'PALB2', 'source_gene': 'invented'})
    FieldConceptMapping.objects.filter(field_name='genomics_palb2').update(status='rejected')
    response = client_for(staff).patch(f'/api/v1/patient-records/{person.pk}/',
        {'genomics_palb1': [{'gene': 'PALB1'}]}, format='json')
    assert response.status_code == 400
    assert list_variants(person) == []


def test_forward_data_migration_preserves_mapping_ids_reviews_repeated_cache_rows_and_facts(setup):
    person, record, _ = setup
    first = legacy_finding(person)
    second = legacy_finding(person)
    mapping = FieldConceptMapping.objects.get(field_name='genomics_palb2')
    FieldConceptMapping.objects.filter(pk=mapping.pk).update(
        field_name='genomics_palb1', source_value='reviewed:palb', status='rejected',
        notes='Keep my decision', provenance='curator')
    original_mapping = FieldConceptMapping.objects.filter(pk=mapping.pk).values().get()
    rows = [{'id': pk, 'gene': 'PALB1', 'marker_key': 'palb1', 'variant': 'PALB1 literal'} for pk in (first, second)]
    PatientRecord.objects.filter(pk=record.pk).update(genomics_palb2=rows, genetic_mutations=rows,
                                                      user_edited_fields=['genomics_palb1'])
    measurements = list(Measurement.objects.filter(person=person).values())
    observations = list(Observation.objects.filter(person=person).values())
    editor = SimpleNamespace(connection=connection)
    migration.rename_projection(apps, editor)
    migration.rename_projection(apps, editor)
    call_command('seed_genomics_catalog')
    call_command('seed_genomics_catalog')
    assert FieldConceptMapping.objects.filter(pk=mapping.pk).values().get() == {
        **original_mapping, 'field_name': 'genomics_palb2'}
    assert not FieldConceptMapping.objects.filter(field_name='genomics_palb1').exists()
    record.refresh_from_db()
    assert {v['id'] for v in record.genomics_palb2} == {first, second}
    assert all(v['gene'] == 'PALB2' and v['source_gene'] == 'PALB1' for v in record.genetic_mutations)
    assert record.user_edited_fields == ['genomics_palb2']
    assert list(Measurement.objects.filter(person=person).values()) == measurements
    assert list(Observation.objects.filter(person=person).values()) == observations


@pytest.mark.django_db(transaction=True)
def test_schema_and_data_upgrade_from_old_named_column():
    from django.db.migrations.state import ProjectState
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory()
    rows = [{'id': 101, 'gene': 'PALB1', 'marker_key': 'palb1', 'variant': 'literal'},
            {'id': 102, 'gene': 'PALB1', 'marker_key': 'palb1', 'variant': 'literal'}]
    PatientRecord.objects.filter(pk=record.pk).update(genomics_palb2=rows)
    current = ProjectState.from_apps(apps)
    old = current.clone()
    old.rename_field('omop_core', 'patientrecord', 'genomics_palb2', 'genomics_palb1')
    current_model = current.apps.get_model('omop_core', 'PatientRecord')
    old_model = old.apps.get_model('omop_core', 'PatientRecord')
    with connection.schema_editor() as editor:
        editor.alter_field(current_model, current_model._meta.get_field('genomics_palb2'),
                           old_model._meta.get_field('genomics_palb1'))
    try:
        assert old_model.objects.get(pk=record.pk).genomics_palb1 == rows
        with connection.schema_editor() as editor:
            migration.Migration('0233_genomics_palb2_naming', 'omop_core').apply(old, editor)
        record.refresh_from_db()
        assert [row['id'] for row in record.genomics_palb2] == [101, 102]
        assert all(row['gene'] == 'PALB2' for row in record.genomics_palb2)
    finally:
        with connection.cursor() as cursor:
            columns = {c.name for c in connection.introspection.get_table_description(cursor, 'patient_record')}
        if 'genomics_palb1' in columns:
            with connection.schema_editor() as editor:
                editor.alter_field(old_model, old_model._meta.get_field('genomics_palb1'),
                                   current_model._meta.get_field('genomics_palb2'))



def test_parent_only_legacy_gene_keeps_exact_source_case(setup):
    person, _, _ = setup
    pk = legacy_finding(person)
    mapping = FieldConceptMapping.objects.get(field_name='genetic_mutations.gene')
    model = Measurement if mapping.omop_table == 'measurement' else Observation
    model.objects.filter(**{mapping.omop_table + '_event_id': pk,
                           mapping.omop_table + '_source_value': mapping.source_value}).delete()
    Measurement.objects.filter(pk=pk).update(qualifier_source_value='PaLb1')
    row = list_variants(person)[0]
    assert row['gene'] == 'PALB2'
    assert row['source_gene'] == 'PaLb1'
