"""0297: specimen, method and pathologist notes per genetic test (#1865)."""
from importlib import import_module

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection

from omop_core.models import CustomPatientField, FieldConceptMapping
from omop_core.services.genomics import mapped_concept
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0297_genetic_test_specimen_and_notes')


def run():
    with connection.schema_editor() as editor:
        migration.forwards(apps, editor)


def specimen_mapping(**changes):
    call_command('seed_genomics_catalog')
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.specimen_type').update(
        status='proposed', concept=None, vocabulary_id='', concept_code='', **changes)
    return FieldConceptMapping.objects.get(field_name='genetic_mutations.specimen_type')


def test_proposed_specimen_type_is_approved_with_the_snomed_observable():
    concept = ConceptFactory(vocabulary__vocabulary_id='SNOMED', concept_code='371439000',
                             domain__domain_id='Observation')
    row = specimen_mapping()
    run()
    row.refresh_from_db()
    assert (row.status, row.vocabulary_id, row.concept_code, row.omop_table) == (
        'approved', 'SNOMED', '371439000', 'observation')
    assert mapped_concept(row)[0] == concept.pk


@pytest.mark.parametrize('status', ['approved', 'rejected'])
def test_a_curator_decision_on_specimen_type_is_left_alone(status):
    ConceptFactory(vocabulary__vocabulary_id='SNOMED', concept_code='371439000', domain__domain_id='Observation')
    row = specimen_mapping(provenance='curator')
    FieldConceptMapping.objects.filter(pk=row.pk).update(status=status)
    before = FieldConceptMapping.objects.filter(pk=row.pk).values().get()
    run()
    assert FieldConceptMapping.objects.filter(pk=row.pk).values().get() == before


def test_pathologist_notes_use_the_loinc_laboratory_comment_once_loaded():
    concept = ConceptFactory(concept_code='77202-0', domain__domain_id='Observation')
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.pathologist_notes').delete()
    run()
    row = FieldConceptMapping.objects.get(field_name='genetic_mutations.pathologist_notes')
    assert (row.status, row.omop_table, row.source_value, row.concept_id) == (
        'approved', 'observation', 'genomics:pathologist_notes', concept.pk)


def test_the_unused_genetic_test_notes_mapping_is_removed_unless_a_custom_field_uses_it():
    old = FieldConceptMapping.objects.create(
        field_name='genetic_test.pathologist_notes', omop_table='observation',
        source_value='phr:genetic-test-pathologist-notes', value_kind='string', status='approved')
    run()
    assert not FieldConceptMapping.objects.filter(pk=old.pk).exists()

    kept = FieldConceptMapping.objects.create(
        field_name='genetic_test.pathologist_notes', omop_table='observation',
        source_value='phr:genetic-test-pathologist-notes', value_kind='string', status='approved')
    CustomPatientField.objects.create(field_name='genetic_test_notes', display_name='Notes', tab='disease',
                                      field_type='text', mode='editable', mapping=kept)
    run()
    assert FieldConceptMapping.objects.filter(pk=kept.pk).exists()
