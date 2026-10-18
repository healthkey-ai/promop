"""PHR D03: domain-correct narratives belong to one patient's diagnosis."""
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, Note, Observation
from omop_core.services.condition_context import condition_contexts
from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, signed_in

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0298_condition_context_mappings')


def run_migration():
    with connection.schema_editor() as editor:
        migration.forwards(apps, editor)


@pytest.fixture
def setup(settings):
    settings.CELERY_BROKER_URL = ''
    note = ConceptFactory(concept_code='48767-8', domain__domain_id='Measurement')
    team = ConceptFactory(concept_code='85847-2', domain__domain_id='Observation')
    event = ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='CDM183',
                           concept_name='condition_occurrence.condition_occurrence_id', domain__domain_id='Metadata')
    run_migration()
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma')
    dx = condition(record, icd10('C90.0', 'Multiple myeloma'), '2020-01-01')
    return record, dx, signed_in(record), note, team, event


def url(dx):
    return f'/api/v1/conditions/{dx.pk}/context/'


def test_migration_assigns_correct_domains_and_is_repeatable(setup):
    *_, note, team, event = setup
    before = list(FieldConceptMapping.objects.filter(field_name__startswith='condition.').values('field_name', 'concept_id'))
    run_migration()
    assert list(FieldConceptMapping.objects.filter(field_name__startswith='condition.').values('field_name', 'concept_id')) == before
    assert FieldConceptMapping.objects.get(field_name='condition.note').concept_id == note.pk
    assert FieldConceptMapping.objects.get(field_name='condition.note').omop_table == 'measurement'
    assert FieldConceptMapping.objects.get(field_name='condition.care_team').concept_id == team.pk


def test_migration_keeps_portable_codes_without_installing_fake_concepts(setup):
    FieldConceptMapping.objects.filter(field_name__startswith='condition.').update(concept=None)
    ConceptFactory._meta.model.objects.filter(concept_code='48767-8').update(domain_id='Observation')
    run_migration()
    mapping = FieldConceptMapping.objects.get(field_name='condition.note')
    assert mapping.concept_id is None
    assert (mapping.vocabulary_id, mapping.concept_code) == ('LOINC', '48767-8')
    record, dx, client, *_ = setup
    assert client.patch(url(dx), {'note': 'Comment'}, format='json').status_code == 400
    assert not Measurement.objects.filter(measurement_event_id=dx.pk).exists()


def test_migration_preserves_explicit_curator_approval(setup):
    custom = ConceptFactory(domain__domain_id='Measurement')
    FieldConceptMapping.objects.filter(field_name='condition.note').update(concept=custom, provenance='curator')
    before = FieldConceptMapping.objects.filter(field_name='condition.note').values().get()
    run_migration()
    assert FieldConceptMapping.objects.filter(field_name='condition.note').values().get() == before


def test_patch_stores_full_text_in_correct_tables_and_exposes_primary_context(setup):
    record, dx, client, note, team, event = setup
    text = 'A diagnosis-specific clinical comment. ' * 30
    response = client.patch(url(dx), {'note': text, 'care_team': 'Dr. Chen, oncology'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data == {'note': text.strip(), 'care_team': 'Dr. Chen, oncology'}
    fact = Measurement.objects.get(measurement_event_id=dx.pk)
    assert fact.measurement_concept_id == note.pk
    assert fact.meas_event_field_concept_id == event.pk
    assert len(fact.value_as_string) <= 60
    assert Note.objects.get(note_source_value=f'condition:measurement:{fact.pk}').note_text == text.strip()
    observation = Observation.objects.get(observation_event_id=dx.pk)
    assert observation.observation_concept_id == team.pk
    assert observation.obs_event_field_concept_id == event.pk
    card = client.get('/api/v1/phr/diagnoses/').data['cancer'][0]
    assert card['id'] == 'primary'
    assert card['condition_id'] == dx.pk
    assert card['note'] == text.strip()
    assert card['care_team'] == 'Dr. Chen, oncology'


def test_patch_preserves_sibling_and_blank_clears_without_resurrecting_history(setup):
    _, dx, client, *_ = setup
    assert client.patch(url(dx), {'note': 'Old note', 'care_team': 'Team'}, format='json').status_code == 200
    assert client.patch(url(dx), {'note': None}, format='json').data == {'note': '', 'care_team': 'Team'}
    assert client.get(url(dx)).data == {'note': '', 'care_team': 'Team'}
    before = Measurement.objects.count()
    client.patch(url(dx), {'note': ''}, format='json')
    assert Measurement.objects.count() == before  # Idempotent retries.


def test_patient_and_condition_isolation_including_typed_event_collisions(setup):
    record, dx, client, note, team, event = setup
    other_record = PatientRecordFactory(disease='')
    other_dx = condition(other_record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    other_client = signed_in(other_record)
    assert other_client.patch(url(other_dx), {'note': 'Private'}, format='json').status_code == 200
    assert client.get(url(other_dx)).status_code in (403, 404)
    assert client.patch(url(other_dx), {'note': 'Stolen'}, format='json').status_code in (403, 404)
    assert client.get(url(dx)).data == {}
    # An event ID alone is not a relationship to a condition.
    wrong_event = ConceptFactory(vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata')
    Observation.objects.create(observation_id=80001, person=record.person, observation_concept=team,
        observation_type_concept_id=32817, observation_date='2026-10-01',
        observation_event_id=dx.pk, obs_event_field_concept=wrong_event,
        observation_source_value='phr:condition-care-team', value_as_string='Wrong event')
    assert condition_contexts(record.person_id, [dx.pk]) == {}


def test_secondary_and_other_cards_receive_only_their_own_context(setup):
    record, primary, client, *_ = setup
    secondary = condition(record, icd10('C50.9', 'Breast cancer'), '2022-01-01')
    other = condition(record, icd10('M10.9', 'Gout'), '2021-01-01')
    for dx, value in ((primary, 'Primary'), (secondary, 'Secondary'), (other, 'Other')):
        assert client.patch(url(dx), {'note': value}, format='json').status_code == 200
    data = client.get('/api/v1/phr/diagnoses/').data
    assert {c['name']: c['note'] for c in data['cancer'] + data['other']} == {
        'Multiple myeloma': 'Primary', 'Breast cancer': 'Secondary', 'Gout': 'Other'}


def test_invalid_patch_is_atomic_and_no_legacy_route_is_registered(setup):
    _, dx, client, *_ = setup
    assert client.patch(url(dx), {'note': 'valid', 'care_team': 'x' * 2001}, format='json').status_code == 400
    assert client.patch(url(dx), {'arbitrary': 'x'}, format='json').status_code == 400
    assert client.get(url(dx)).data == {}
    from django.urls import resolve
    legacy = resolve(f'/api/conditions/{dx.pk}/context/')
    assert getattr(legacy.func, 'actions', {}).get('get') != 'context'
    assert APIClient().get(url(dx)).status_code in (401, 403)


def test_erroneous_facts_and_foreign_overflow_notes_are_not_exposed(setup):
    record, dx, client, *_ = setup
    client.patch(url(dx), {'note': 'Narrative' * 50}, format='json')
    fact = Measurement.objects.get(measurement_event_id=dx.pk)
    Note.objects.filter(note_source_value=f'condition:measurement:{fact.pk}').update(person=PatientRecordFactory().person)
    # Never dereference another patient's note, even if a fact points to it.
    assert client.get(url(dx)).data['note'] == fact.value_as_string
    Measurement.objects.filter(pk=fact.pk).update(is_erroneous=True)
    assert client.get(url(dx)).data == {}


def test_legacy_unmapped_context_and_alternate_cdm_version_remain_readable(setup):
    record, dx, client, *_ = setup
    alternate = ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='CDM709',
        concept_name='condition_occurrence.condition_occurrence_id', domain__domain_id='Metadata')
    Observation.objects.create(observation_id=80002, person=record.person, observation_concept_id=0,
        observation_type_concept_id=32817, observation_date='2020-01-01',
        observation_event_id=dx.pk, obs_event_field_concept=alternate,
        observation_source_value='phr:condition-note', value_as_string='Legacy note')
    assert client.get(url(dx)).data == {'note': 'Legacy note'}
    assert client.patch(url(dx), {'note': 'Updated'}, format='json').data == {'note': 'Updated'}


def test_diagnosis_annotations_are_excluded_from_labs_and_lab_activity(setup):
    record, dx, client, *_ = setup
    assert client.patch(url(dx), {'note': 'A comment'}, format='json').status_code == 200
    from patient_portal.api.phr.labs import lab_history, labs
    from patient_portal.api.phr.feed import whats_new
    assert labs(record.person, record)['tests'] == []
    assert lab_history(record.person, setup[3].pk, record) is None
    assert not any(item['section'] == 'labs' for group in whats_new(record.person, record)['groups'] for item in group['items'])


def test_context_reads_batch_conditions_without_one_query_per_diagnosis(setup, django_assert_num_queries):
    record, dx, client, *_ = setup
    client.patch(url(dx), {'note': 'A note'}, format='json')
    many = [dx.pk] + [condition(record, icd10(f'E{i}', f'Condition {i}'), '2020-01-01').pk for i in range(12)]
    with django_assert_num_queries(4):
        assert condition_contexts(record.person_id, many) == {dx.pk: {'note': 'A note'}}


def test_provenance_is_attached_to_written_fact(setup):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord
    record, dx, client, *_ = setup
    response = client.patch(url(dx), {'care_team': 'Team'}, format='json', HTTP_X_PROVENANCE_SOURCE='Patient reported')
    assert response.status_code == 200
    fact = Observation.objects.get(observation_event_id=dx.pk)
    assert ProvenanceRecord.objects.filter(content_type=ContentType.objects.get_for_model(Observation), object_id=fact.pk).exists()


def test_shared_diagnoses_include_context_only_for_selected_cards(setup, settings):
    from tests.test_phr_sharing import grant, holder
    settings.PHR_SHARE_URL = 'https://one.example/r'
    record, dx, client, *_ = setup
    other = condition(record, icd10('M10.9', 'Gout'), '2021-01-01')
    client.patch(url(dx), {'note': 'Private cancer context'}, format='json')
    client.patch(url(other), {'note': 'Shared gout context'}, format='json')
    share = grant(client, selection={'diagnoses': {'items': [f'condition-{other.pk}']}})
    assert share.status_code == 201
    data = holder(share.data).get('/api/v1/phr/shared/diagnoses/').data
    assert data['cancer'] == []
    assert len(data['other']) == 1
    assert data['other'][0]['note'] == 'Shared gout context'


def test_missing_event_vocabulary_rejects_write_instead_of_untyped_link(setup):
    record, dx, client, *_, event = setup
    type(event).objects.filter(pk=event.pk).delete()
    response = client.patch(url(dx), {'note': 'A note', 'care_team': 'Team'}, format='json')
    assert response.status_code == 400
    assert not Measurement.objects.filter(measurement_event_id=dx.pk).exists()
    assert not Observation.objects.filter(observation_event_id=dx.pk).exists()
