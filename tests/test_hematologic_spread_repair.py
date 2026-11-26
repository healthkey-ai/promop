"""Hematologic Spread has a concept and belongs to the blood cancers (migration 0376)."""
import pytest

from omop_core.models import (
    CustomPatientField, DiseaseCategoryChoice, FieldConceptMapping, FieldDiseaseMembership,
)
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory
from tests.migration_utils import run_data_migration

pytestmark = pytest.mark.django_db

MIGRATION = '0376_hematologic_spread_concept_and_scope'


@pytest.fixture
def field():
    """The field as migration 0293 left it: approved, observation, no concept, scoped to nobody."""
    mapping = FieldConceptMapping.objects.create(
        field_name='hematologic_spread', omop_table='observation', status='approved',
        source_value='phr:hematologic-spread', value_kind='string', provenance='system_generated')
    CustomPatientField.objects.create(
        field_name='hematologic_spread', display_name='Hematologic Spread', tab='disease',
        field_type='text', mode='editable', mapping=mapping)
    for code in ('MM', 'FL', 'MCL', 'CLL', 'DLBCL', 'BC'):
        DiseaseCategoryChoice.objects.get_or_create(code=code, defaults={'label': code})
    return mapping


def observation_concept(concept_id, name, vocabulary, code):
    return ConceptFactory(
        concept_id=concept_id, concept_name=name, concept_code=code,
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary, vocabulary_name=vocabulary),
        domain=DomainFactory(domain_id='Observation', domain_name='Observation'),
        concept_class=ConceptClassFactory(concept_class_id='Clinical Observation',
                                          concept_class_name='Clinical Observation'))


def sites_concept():
    return observation_concept(3042814, 'Sites of distant metastasis', 'LOINC', '33733-7')


def scope():
    return set(FieldDiseaseMembership.objects.filter(field_name='hematologic_spread')
               .values_list('disease', flat=True))


def test_it_gets_the_concept_and_the_blood_cancers(field):
    concept = sites_concept()
    run_data_migration(MIGRATION, 'repair')
    field.refresh_from_db()
    assert (field.concept_id, field.vocabulary_id, field.concept_code) == (concept.pk, 'LOINC', '33733-7')
    assert (field.omop_table, field.status) == ('observation', 'approved')
    assert scope() == {'MM', 'FL', 'MCL', 'CLL', 'DLBCL'}      # not BC, and so not prostate


def test_a_vocabulary_not_loaded_yet_still_records_the_code_for_the_later_resolve(field):
    run_data_migration(MIGRATION, 'repair')
    field.refresh_from_db()
    assert (field.concept_id, field.vocabulary_id, field.concept_code) == (None, 'LOINC', '33733-7')


def test_a_curators_concept_and_scope_are_left_alone(field):
    sites_concept()
    other = observation_concept(4299435, 'Site of distant metastasis', 'SNOMED', '385421009')
    FieldConceptMapping.objects.filter(pk=field.pk).update(
        concept=other, vocabulary_id='SNOMED', concept_code='385421009')
    FieldDiseaseMembership.objects.create(field_name='hematologic_spread', disease='MM', provenance='curator')
    run_data_migration(MIGRATION, 'repair')
    field.refresh_from_db()
    assert (field.concept_id, field.vocabulary_id) == (other.pk, 'SNOMED')
    assert scope() == {'MM'}


def test_it_is_repeatable_and_an_instance_without_the_field_is_untouched():
    run_data_migration(MIGRATION, 'repair')
    assert scope() == set()


def test_running_twice_changes_nothing(field):
    sites_concept()
    run_data_migration(MIGRATION, 'repair')
    run_data_migration(MIGRATION, 'repair')
    assert FieldDiseaseMembership.objects.filter(field_name='hematologic_spread').count() == 5
