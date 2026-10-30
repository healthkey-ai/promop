"""PHR D08/D18: medication attributes follow typed exposure IDs, not names."""
from datetime import date
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection
from rest_framework.test import APIClient

from omop_core.models import Concept, DrugExposure, FieldConceptMapping, Note, Observation, PatientRecord
from omop_core.services.medication_context import medication_contexts
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import EpisodeEvent
from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_phr_read_model import drug, signed_in, therapy_line

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0317_medication_context_mappings')


def migrate():
    with connection.schema_editor() as editor:
        migration.forwards(apps, editor)


@pytest.fixture
def setup(settings):
    settings.CELERY_BROKER_URL = ''
    role = ConceptFactory(concept_code='104803-2', domain__domain_id='Observation')
    category = ConceptFactory(concept_code='104747-1', domain__domain_id='Observation')
    event = ConceptFactory(concept_id=1147094, vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata',
                           concept_code='CDM150', concept_name='drug_exposure.drug_exposure_id')
    ConceptFactory(concept_id=32865, concept_code='32865', vocabulary__vocabulary_id='Type Concept', domain__domain_id='Type Concept')
    migrate()
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma')
    exposure = drug(record, ConceptFactory(concept_name='Lenalidomide'), '2024-01-01')
    return record, exposure, signed_in(record), role, category, event


def url(exposure):
    return f'/api/v1/patient-records/{exposure.person_id}/'


def test_migration_repairs_domains_repeatably_and_preserves_curator(setup):
    *_, role, category, event = setup
    wrong = ConceptFactory(domain__domain_id='Procedure')
    FieldConceptMapping.objects.filter(field_name='drug_exposure.role').update(concept=wrong)
    migrate()
    mapping = FieldConceptMapping.objects.get(field_name='drug_exposure.role')
    assert (mapping.concept_id, mapping.omop_table, mapping.concept_code) == (role.pk, 'observation', '104803-2')
    before = list(FieldConceptMapping.objects.filter(field_name__startswith='drug_exposure.').values())
    migrate()
    assert [(r['field_name'], r['concept_id']) for r in before] == list(
        FieldConceptMapping.objects.filter(field_name__startswith='drug_exposure.').values_list('field_name', 'concept_id'))
    FieldConceptMapping.objects.filter(pk=mapping.pk).update(concept=wrong, provenance='curator')
    before = FieldConceptMapping.objects.filter(pk=mapping.pk).values().get()
    migrate()
    assert FieldConceptMapping.objects.filter(pk=mapping.pk).values().get() == before


def test_portable_mapping_without_loaded_vocab_refuses_fact_write(setup):
    _, exposure, client, role, *_ = setup
    Concept.objects.filter(pk=role.pk).update(domain_id='Measurement')
    migrate()
    mapping = FieldConceptMapping.objects.get(field_name='drug_exposure.role')
    assert mapping.concept_id is None
    assert (mapping.vocabulary_id, mapping.concept_code) == ('LOINC', '104803-2')
    response = patch(client, exposure, {'category': 'Supportive', 'role': 'Induction'})
    assert response.status_code == 200, response.data
    assert saved(response, exposure)['role'] == 'Induction'
    assert 'medication_contexts' in response.data['user_edited_fields']
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()




def patch(client, exposure, attributes):
    return client.patch(url(exposure), {'medication_contexts': {str(exposure.pk): attributes}}, format='json')


def saved(response, exposure):
    return response.data['medication_contexts'][str(exposure.pk)]


def current(client, exposure):
    return client.get(url(exposure)).data['patient_info']['medication_contexts'].get(str(exposure.pk), {})


def test_save_lands_on_patient_record_before_forward_projection_without_derive(setup, monkeypatch):
    from omop_core.services import medication_context as service, patient_record_service
    record, exposure, client, role, category, event = setup
    original = service.save_medication_context
    def project(row, values, **kwargs):
        stored = PatientRecord.objects.get(pk=record.pk)
        assert stored.medication_contexts[str(exposure.pk)] == {'role': 'Induction', 'category': 'Myeloma treatment'}
        return original(row, values, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('An interactive save must not derive or load an OMOP snapshot')
    monkeypatch.setattr(service, 'save_medication_context', project)
    monkeypatch.setattr(patient_record_service, 'refresh_patient_record', forbidden)
    monkeypatch.setattr(patient_record_service, '_build_snapshot', forbidden)
    before = (record.derived_at, record.derivation_version)
    response = patch(client, exposure, {'role': 'Induction', 'category': 'Myeloma treatment'})
    assert response.status_code == 200, response.data
    assert saved(response, exposure) == {'role': 'Induction', 'category': 'Myeloma treatment'}
    record.refresh_from_db()
    assert (record.derived_at, record.derivation_version) == before
    facts = Observation.objects.filter(observation_event_id=exposure.pk)
    assert set(facts.values_list('observation_concept_id', flat=True)) == {role.pk, category.pk}
    assert set(facts.values_list('obs_event_field_concept_id', flat=True)) == {event.pk}
    assert set(facts.values_list('observation_type_concept_id', flat=True)) == {32865}
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord, RecordRevision
    assert ProvenanceRecord.objects.filter(content_type=ContentType.objects.get_for_model(Observation),
                                            object_id__in=facts.values('pk'), source='PATIENT_SELF').count() == 2
    assert RecordRevision.objects.filter(patient_record=record, field='medication_contexts').exists()


def test_projection_failure_keeps_saved_value_and_phr_display(setup, monkeypatch):
    from omop_core.services import medication_context as service
    _, exposure, client, *_ = setup
    def fail(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 / 0')
    monkeypatch.setattr(service, 'save_medication_context', fail)
    response = patch(client, exposure, {'category': 'Supplement'})
    assert response.status_code == 200, response.data
    assert saved(response, exposure)['category'] == 'Supplement'
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()
    item = client.get('/api/v1/phr/medications/').data['medications'][0]
    assert item['category'] == 'Supplement'
    assert item['drug_exposure_id'] == exposure.pk


def test_partial_clear_retry_and_overflow_are_lossless(setup):
    _, exposure, client, *_ = setup
    text = 'Long medication purpose. ' * 25
    assert patch(client, exposure, {'role': '[note:123]', 'category': text}).status_code == 200
    assert current(client, exposure) == {'role': '[note:123]', 'category': text.strip()}
    assert Note.objects.filter(note_source_value__startswith='medication:observation:').count() == 2
    response = patch(client, exposure, {'role': None})
    assert saved(response, exposure) == {'role': '', 'category': text.strip()}
    count = Observation.objects.count()
    assert patch(client, exposure, {'role': ''}).status_code == 200
    assert Observation.objects.count() == count
    assert current(client, exposure)['role'] == ''


def test_only_changed_attributes_project_when_editor_echoes_full_json(setup):
    record, exposure, client, *_ = setup
    other = drug(record, exposure.drug_concept, '2025-01-01')
    patch(client, exposure, {'role': 'Induction'})
    response = patch(client, other, {'role': 'Maintenance'})
    before = Observation.objects.count()
    values = response.data['medication_contexts']
    values[str(other.pk)]['category'] = 'Myeloma treatment'
    response = client.patch(url(exposure), {'medication_contexts': values}, format='json')
    assert response.status_code == 200, response.data
    assert Observation.objects.count() == before + 1
    assert current(client, exposure)['role'] == 'Induction'


def test_patient_and_exposure_ownership_are_validated_before_saving(setup):
    record, exposure, client, *_ = setup
    other = PatientRecordFactory()
    foreign = drug(other, exposure.drug_concept, '2024-01-01')
    assert patch(client, foreign, {'role': 'Stolen'}).status_code in (403, 404)
    response = client.patch(url(exposure), {'medication_contexts': {str(foreign.pk): {'role': 'Stolen'}}}, format='json')
    assert response.status_code == 400
    assert current(client, exposure) == {}
    DrugExposure.objects.filter(pk=exposure.pk).update(is_erroneous=True)
    assert patch(client, exposure, {'role': 'Invalid'}).status_code == 400


@pytest.mark.parametrize('value', [[], {'invalid': {}}, {'0': {}}, {'9223372036854775808': {}},
                                  {'1': {'intent': 'Curative'}}, {'1': {'role': 'x' * 1001}}])
def test_invalid_json_is_rejected_atomically(setup, value):
    _, exposure, client, *_ = setup
    assert client.patch(url(exposure), {'medication_contexts': value}, format='json').status_code == 400
    assert current(client, exposure) == {}
    assert APIClient().get(url(exposure)).status_code in (401, 403)


def test_same_drug_in_different_lines_retains_distinct_roles_and_line_intent(setup):
    from omop_core.services.episode_service import author_therapy_line
    from tests.test_episode_service import _seed_episode_writer_concepts
    record, original, client, *_ = setup
    _seed_episode_writer_concepts()
    exposures = []
    for number, role in ((1, 'Induction'), (2, 'Maintenance')):
        line = author_therapy_line(record.person, line_number=number, start_date=date(2024 + number, 1, 1),
                                  drugs=[{'concept_id': original.drug_concept_id}], intent='Curative')
        exposure = DrugExposure.objects.get(pk=line.drug_exposure_ids[0])
        exposures.append(exposure)
        assert patch(client, exposure, {'role': role}).status_code == 200
    lines = client.get('/api/v1/phr/therapy/').data['tracks'][0]['lines']
    assert [line['intent'] for line in lines] == ['Curative', 'Curative']
    assert [line['medications'][0]['role'] for line in lines] == ['Induction', 'Maintenance']
    assert [line['medications'][0]['exposures'][0]['drug_exposure_id'] for line in lines] == [e.pk for e in exposures]


def test_conflicting_roles_within_a_line_keep_exposure_identity(setup):
    record, _, client, *_ = setup
    line = therapy_line(record, 1, '2024-01-01', '2024-12-31', [
        ('Melphalan', '2024-01-01', '2024-02-01'), ('Melphalan', '2024-11-01', '2024-12-01')])
    ids = list(EpisodeEvent.objects.filter(episode_id=line.pk).values_list('event_id', flat=True))
    for pk, role in zip(ids, ('Induction', 'Conditioning')):
        assert patch(client, DrugExposure.objects.get(pk=pk), {'role': role, 'category': 'Myeloma treatment'}).status_code == 200
    med, = client.get('/api/v1/phr/therapy/').data['tracks'][0]['lines'][0]['medications']
    assert 'role' not in med and med['category'] == 'Myeloma treatment'
    assert {entry['role'] for entry in med['exposures']} == {'Induction', 'Conditioning'}


def test_legacy_sources_typed_namespace_and_foreign_notes(setup, django_assert_num_queries):
    record, exposure, client, _, category, event = setup
    alternate = ConceptFactory(vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata',
                               concept_code='drug_exposure.drug_exposure_id')
    with suppress_patient_record_refresh():
        fact = Observation.objects.create(observation_id=80002, person=record.person, observation_concept_id=0,
            observation_type_concept_id=32817, observation_date='2020-01-01', observation_event_id=exposure.pk,
            obs_event_field_concept=alternate, observation_source_value='phr:drug-exposure-role', value_as_string='Legacy induction')
    many = [exposure.pk] + [drug(record, exposure.drug_concept, '2023-01-01').pk for _ in range(12)]
    with django_assert_num_queries(2):
        assert medication_contexts(record.person_id, many) == {exposure.pk: {'role': 'Legacy induction'}}
    Observation.objects.filter(pk=fact.pk).update(obs_event_field_concept=ConceptFactory(domain__domain_id='Metadata'))
    assert medication_contexts(record.person_id, many) == {}
    assert patch(client, exposure, {'role': 'Long role. ' * 30}).status_code == 200
    fact = Observation.objects.filter(observation_event_id=exposure.pk).exclude(pk=fact.pk).get()
    Note.objects.filter(note_source_value=f'medication:observation:{fact.pk}').update(person=PatientRecordFactory().person)
    assert medication_contexts(record.person_id, [exposure.pk])[exposure.pk]['role'] == fact.value_as_string


def test_mapping_approval_without_authorship_keeps_patient_answer_pending(setup):
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.omop_projection import project_field_to_omop
    record, exposure, client, *_ = setup
    FieldConceptMapping.objects.filter(field_name='drug_exposure.category').update(status='proposed')
    response = patch(client, exposure, {'category': 'Supportive'})
    assert response.status_code == 200 and not Observation.objects.filter(observation_event_id=exposure.pk).exists()
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.medication_contexts[str(exposure.pk)]['category'] == 'Supportive'
    assert 'medication_contexts' in record.user_edited_fields
    FieldConceptMapping.objects.filter(field_name='drug_exposure.category').update(status='approved')
    mapping = FieldConceptMapping.objects.get(field_name='drug_exposure.category')
    assert project_field_to_omop(mapping) == 0
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()
    assert client.get('/api/v1/phr/medications/').data['medications'][0]['category'] == 'Supportive'
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert 'medication_contexts' in record.user_edited_fields
    assert record.medication_contexts[str(exposure.pk)]['category'] == 'Supportive'


def test_repeated_mapping_approval_never_duplicates_self_report_as_ehr(setup):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord
    from omop_core.services.omop_projection import project_field_to_omop
    from omop_core.services.patient_record_service import refresh_patient_record
    record, exposure, client, *_ = setup
    assert patch(client, exposure, {'role': 'Induction'}).status_code == 200
    record.refresh_from_db()
    assert 'medication_contexts' in record.user_edited_fields
    mapping = FieldConceptMapping.objects.get(field_name='drug_exposure.role')
    for _ in range(2):
        assert project_field_to_omop(mapping) == 0
    fact, = Observation.objects.filter(observation_event_id=exposure.pk)
    assert fact.observation_type_concept_id == 32865
    assert ProvenanceRecord.objects.filter(content_type=ContentType.objects.get_for_model(Observation),
                                            object_id=fact.pk, source='PATIENT_SELF').exists()
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert 'medication_contexts' not in record.user_edited_fields


def test_mapping_approval_does_not_invent_authorship_for_legacy_pending_json(setup):
    from omop_core.services.omop_projection import project_field_to_omop
    record, exposure, *_ = setup
    values = {str(exposure.pk): {'role': 'Induction'}}
    PatientRecord.objects.filter(pk=record.pk).update(
        medication_contexts=values, user_edited_fields=['medication_contexts'])
    mapping = FieldConceptMapping.objects.get(field_name='drug_exposure.role')
    assert project_field_to_omop(mapping) == 0
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()
    record.refresh_from_db()
    assert record.medication_contexts == values
    assert 'medication_contexts' in record.user_edited_fields


def test_shared_medication_selection_limits_attributes(setup, settings):
    from tests.test_phr_sharing import grant, holder
    from patient_portal.api.phr.sharing import medication_group
    settings.PHR_SHARE_URL = 'https://one.example/r'
    record, exposure, client, *_ = setup
    patch(client, exposure, {'category': 'Myeloma treatment'})
    other = drug(record, ConceptFactory(concept_name='Vitamin D'), '2024-01-01', '2024-12-31')
    patch(client, other, {'category': 'Supplement'})
    meds = client.get('/api/v1/phr/medications/').data['medications']
    selected = next(item for item in meds if item['drug_exposure_id'] == other.pk)
    share = grant(client, selection={'medications': {'items': [medication_group(selected)]}})
    shared = holder(share.data)
    assert [item['category'] for item in shared.get('/api/v1/phr/shared/medications/').data['medications']] == ['Supplement']
    assert shared.get(f'/api/v1/phr/shared/medications/{exposure.drug_concept_id}/').status_code == 404


def test_missing_fact_type_keeps_the_patient_record_write(setup):
    _, exposure, client, *_ = setup
    Concept.objects.filter(pk__in=[32865, 32817]).delete()
    response = patch(client, exposure, {'role': 'Induction'})
    assert response.status_code == 200, response.data
    assert saved(response, exposure)['role'] == 'Induction'
    assert 'medication_contexts' in response.data['user_edited_fields']
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()


def test_unchanged_retired_echo_does_not_block_another_edit(setup):
    record, exposure, client, *_ = setup
    first = patch(client, exposure, {'role': 'Induction'})
    values = first.data['medication_contexts']
    DrugExposure.objects.filter(pk=exposure.pk).update(is_erroneous=True)
    other = drug(record, exposure.drug_concept, '2025-01-01')
    values[str(other.pk)] = {'role': 'Maintenance'}
    response = client.patch(url(exposure), {'medication_contexts': values}, format='json')
    assert response.status_code == 200, response.data
    assert saved(response, other)['role'] == 'Maintenance'
