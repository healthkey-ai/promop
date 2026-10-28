"""PHR D08/D18: medication attributes follow typed exposure IDs, not names."""
from datetime import date
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection
from rest_framework.test import APIClient

from omop_core.models import Concept, DrugExposure, FieldConceptMapping, Note, Observation
from omop_core.services.medication_context import medication_contexts
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import EpisodeEvent
from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_phr_read_model import drug, signed_in, therapy_line

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0316_medication_context_mappings')


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
    migrate()
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma')
    exposure = drug(record, ConceptFactory(concept_name='Lenalidomide'), '2024-01-01')
    return record, exposure, signed_in(record), role, category, event


def url(exposure):
    return f'/api/v1/drug-exposures/{exposure.pk}/context/'


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
    assert client.patch(url(exposure), {'category': 'Supportive', 'role': 'Induction'}, format='json').status_code == 400
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()


def test_patch_is_typed_auditable_and_exposed_in_summary_and_history(setup):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord
    record, exposure, client, role, category, event = setup
    response = client.patch(url(exposure), {'role': 'Induction', 'category': 'Myeloma treatment'},
                            format='json', HTTP_X_PROVENANCE_SOURCE='Patient reported')
    assert response.status_code == 200, response.data
    assert response.data == {'role': 'Induction', 'category': 'Myeloma treatment'}
    facts = Observation.objects.filter(observation_event_id=exposure.pk)
    assert set(facts.values_list('observation_concept_id', flat=True)) == {role.pk, category.pk}
    assert set(facts.values_list('obs_event_field_concept_id', flat=True)) == {event.pk}
    assert ProvenanceRecord.objects.filter(content_type=ContentType.objects.get_for_model(Observation),
                                            object_id__in=facts.values('pk')).count() == 2
    summary = client.get('/api/v1/phr/medications/').data['medications'][0]
    assert summary['drug_exposure_id'] == exposure.pk
    assert summary['role'] == 'Induction' and summary['category'] == 'Myeloma treatment'
    detail = client.get(f"/api/v1/phr/medications/{summary['id']}/").data
    assert detail['history'][0]['role'] == 'Induction'
    assert detail['history'][0]['drug_exposure_id'] == exposure.pk


def test_partial_clear_retry_and_overflow_are_lossless(setup):
    _, exposure, client, *_ = setup
    text = 'Long medication purpose. ' * 25
    assert client.patch(url(exposure), {'role': '[note:123]', 'category': text}, format='json').status_code == 200
    assert client.get(url(exposure)).data == {'role': '[note:123]', 'category': text.strip()}
    assert Note.objects.filter(note_source_value__startswith='medication:observation:').count() == 2
    assert client.patch(url(exposure), {'role': None}, format='json').data == {'role': '', 'category': text.strip()}
    count = Observation.objects.count()
    client.patch(url(exposure), {'role': ''}, format='json')
    assert Observation.objects.count() == count
    assert client.get(url(exposure)).data['role'] == ''


def test_patient_and_event_namespace_isolation_and_erroneous_facts(setup):
    record, exposure, client, _, category, event = setup
    other = PatientRecordFactory()
    other_exposure = drug(other, exposure.drug_concept, '2024-01-01')
    assert signed_in(other).patch(url(other_exposure), {'role': 'Private'}, format='json').status_code == 200
    assert client.get(url(other_exposure)).status_code in (403, 404)
    assert client.patch(url(other_exposure), {'role': 'Overwrite'}, format='json').status_code in (403, 404)
    with suppress_patient_record_refresh():
        fact = Observation.objects.create(observation_id=80001, person=record.person, observation_concept=category,
            observation_type_concept_id=32817, observation_date='2020-01-01', observation_event_id=exposure.pk,
            obs_event_field_concept=ConceptFactory(domain__domain_id='Metadata'),
            observation_source_value='phr:drug-exposure-category', value_as_string='Wrong namespace')
    assert client.get(url(exposure)).data == {}
    Observation.objects.filter(pk=fact.pk).update(obs_event_field_concept=event)
    assert client.get(url(exposure)).data == {'category': 'Wrong namespace'}
    Observation.objects.filter(pk=fact.pk).update(is_erroneous=True)
    assert client.get(url(exposure)).data == {}
    DrugExposure.objects.filter(pk=exposure.pk).update(is_erroneous=True)
    assert client.patch(url(exposure), {'role': 'Invalid'}, format='json').status_code in (400, 404)


def test_invalid_input_missing_namespace_and_legacy_routes(setup):
    _, exposure, client, *_, event = setup
    for values in ({'role': 'x' * 1001}, {'role': 'ok', 'category': ['bad']}, {'intent': 'Curative'}):
        assert client.patch(url(exposure), values, format='json').status_code == 400
    assert APIClient().get(url(exposure)).status_code in (401, 403)
    from django.urls import resolve
    assert getattr(resolve(f'/api/drug-exposures/{exposure.pk}/context/').func, 'actions', {}).get('get') != 'context'
    Concept.objects.filter(pk=event.pk).delete()
    assert client.patch(url(exposure), {'role': 'Induction'}, format='json').status_code == 400
    assert not Observation.objects.filter(observation_event_id=exposure.pk).exists()


def test_same_drug_in_different_lines_retains_distinct_attributes(setup):
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
        assert client.patch(url(exposure), {'role': role}, format='json').status_code == 200
    assert exposures[0].pk != exposures[1].pk
    lines = client.get('/api/v1/phr/therapy/').data['tracks'][0]['lines']
    assert [line['intent'] for line in lines] == ['Curative', 'Curative']
    assert [line['medications'][0]['role'] for line in lines] == ['Induction', 'Maintenance']
    assert [line['medications'][0]['exposures'][0]['drug_exposure_id'] for line in lines] == [e.pk for e in exposures]


def test_conflicting_roles_in_one_line_are_exposed_without_collapsing_them(setup):
    record, _, client, *_, event = setup
    line = therapy_line(record, 1, '2024-01-01', '2024-12-31', [
        ('Melphalan', '2024-01-01', '2024-02-01'), ('Melphalan', '2024-11-01', '2024-12-01')])
    ids = list(EpisodeEvent.objects.filter(episode_id=line.pk).values_list('event_id', flat=True))
    for pk, role in zip(ids, ('Induction', 'Conditioning')):
        exposure = DrugExposure.objects.get(pk=pk)
        assert client.patch(url(exposure), {'role': role, 'category': 'Myeloma treatment'}, format='json').status_code == 200
    # A collision from another domain must not attach a medication to this line.
    unrelated = drug(record, ConceptFactory(concept_name='Unrelated'), '2024-06-01')
    EpisodeEvent.objects.create(episode_id=line.pk, event_id=unrelated.pk,
                               episode_event_field_concept=ConceptFactory(domain__domain_id='Metadata'))
    med, = client.get('/api/v1/phr/therapy/').data['tracks'][0]['lines'][0]['medications']
    assert med['name'] == 'Melphalan' and 'role' not in med
    assert med['category'] == 'Myeloma treatment'
    assert {e['role'] for e in med['exposures']} == {'Induction', 'Conditioning'}


def test_legacy_source_identity_alternate_cdm_and_batch_queries(setup, django_assert_num_queries):
    record, exposure, client, *_ = setup
    alternate = ConceptFactory(vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata',
                               concept_code='drug_exposure.drug_exposure_id')
    with suppress_patient_record_refresh():
        Observation.objects.create(observation_id=80002, person=record.person, observation_concept_id=0,
            observation_type_concept_id=32817, observation_date='2020-01-01', observation_event_id=exposure.pk,
            obs_event_field_concept=alternate, observation_source_value='phr:drug-exposure-role',
            value_as_string='Legacy induction')
    many = [exposure.pk] + [drug(record, exposure.drug_concept, '2023-01-01').pk for _ in range(12)]
    with django_assert_num_queries(2):
        assert medication_contexts(record.person_id, many) == {exposure.pk: {'role': 'Legacy induction'}}


def test_unrelated_newer_exposure_does_not_inherit_older_role(setup):
    record, exposure, client, *_ = setup
    client.patch(url(exposure), {'role': 'Induction', 'category': 'Myeloma treatment'}, format='json')
    newer = drug(record, exposure.drug_concept, '2025-01-01')
    summary = client.get('/api/v1/phr/medications/').data['medications'][0]
    assert summary['drug_exposure_id'] == newer.pk
    assert 'role' not in summary and 'category' not in summary
    detail = client.get(f"/api/v1/phr/medications/{summary['id']}/").data
    assert 'role' not in detail['history'][0]
    assert detail['history'][1]['role'] == 'Induction'


def test_shared_medications_expose_only_selected_group_attributes(setup, settings):
    from tests.test_phr_sharing import grant, holder
    settings.PHR_SHARE_URL = 'https://one.example/r'
    record, exposure, client, *_ = setup
    client.patch(url(exposure), {'category': 'Myeloma treatment'}, format='json')
    other = drug(record, ConceptFactory(concept_name='Vitamin D'), '2024-01-01', '2024-12-31')
    client.patch(url(other), {'category': 'Supplement'}, format='json')
    from patient_portal.api.phr.sharing import medication_group
    summary = client.get('/api/v1/phr/medications/').data['medications']
    selected = next(m for m in summary if m['drug_exposure_id'] == other.pk)
    share = grant(client, selection={'medications': {'items': [medication_group(selected)]}})
    assert share.status_code == 201, share.data
    shared = holder(share.data)
    meds = shared.get('/api/v1/phr/shared/medications/').data['medications']
    assert [m['category'] for m in meds] == ['Supplement']
    assert shared.get(f'/api/v1/phr/shared/medications/{exposure.drug_concept_id}/').status_code == 404


def test_foreign_overflow_note_is_never_resolved(setup):
    _, exposure, client, *_ = setup
    client.patch(url(exposure), {'role': 'Long role. ' * 30}, format='json')
    fact = Observation.objects.get(observation_event_id=exposure.pk)
    Note.objects.filter(note_source_value=f'medication:observation:{fact.pk}').update(
        person=PatientRecordFactory().person)
    assert client.get(url(exposure)).data['role'] == fact.value_as_string
