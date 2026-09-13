from datetime import timedelta

import pytest
from django.utils import timezone

from omop_core.models import FieldChoice, FieldConceptMapping, Measurement, Observation
from omop_core.services.breast_cancer import STAGE_QUESTIONS
from omop_core.services.field_values import save_mapping
from omop_core.services.omop_projection import CLEAR_VALUE
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.api.views import PatientRecordViewSet
from tests.factories import ConceptFactory, ObservationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def record():
    for field, codes in STAGE_QUESTIONS.items():
        for code in codes:
            question = ConceptFactory(concept_code=code)
            if codes[code] == 'c':
                FieldConceptMapping.objects.update_or_create(field_name=field, defaults={
                    'concept': question, 'concept_code': code, 'vocabulary_id': 'LOINC',
                    'source_value': code, 'status': 'approved', 'omop_table': 'measurement', 'value_kind': 'string'})
    return PatientRecordFactory(staging_modalities='p')


def write(record, field, value):
    return PatientRecordViewSet._project_mapped_fields(record.person, {field}, {field: value}, record=record)


@pytest.mark.parametrize('basis,expected', [('c: Clinical', '21905-5'), ('p → Pathological', '21899-0'),
                                         ('yp', '21899-0')])
def test_selected_basis_routes_tnm_and_roundtrips(record, basis, expected):
    record.staging_modalities = basis
    record.user_edited_fields = ['staging_modalities']
    record.save()
    assert write(record, 'tumor_stage', 'T1') == {'tumor_stage'}
    row = Measurement.objects.get(person=record.person)
    assert row.measurement_source_value == expected
    assert row.qualifier_source_value == basis.split(':')[0].split(' →')[0]
    refreshed = refresh_patient_record(record.person)
    assert refreshed.tumor_stage == 'T1'
    assert refreshed.staging_modalities == basis
    assert 'staging_modalities' not in refreshed.user_edited_fields


def test_p_and_yp_are_distinct_and_clear_suppresses_legacy_cross_table_history(record):
    legacy = ObservationFactory(person=record.person, observation_id=900000,
        observation_source_value='21899-0', observation_date=timezone.localdate(), value_as_string='T4')
    assert write(record, 'tumor_stage', 'T2') == {'tumor_stage'}
    record.staging_modalities = 'yp'
    record.save()
    assert write(record, 'tumor_stage', 'T1') == {'tumor_stage'}
    assert set(Measurement.objects.filter(person=record.person).values_list('qualifier_source_value', flat=True)) == {'p', 'yp'}
    assert refresh_patient_record(record.person).tumor_stage == 'T1'
    assert write(record, 'tumor_stage', None) == {'tumor_stage'}
    assert refresh_patient_record(record.person).tumor_stage is None
    legacy.refresh_from_db()
    assert legacy.value_as_string == 'T4'
    Observation.objects.filter(pk=legacy.pk).update(observation_date=timezone.localdate() + timedelta(days=1))
    assert refresh_patient_record(record.person).tumor_stage == 'T4'


def test_ambiguous_basis_preserves_pending_value_without_a_fact(record):
    record.staging_modalities = 'c, p'
    record.tumor_stage = 'T1'
    record.user_edited_fields = ['tumor_stage', 'staging_modalities']
    record.save()
    assert write(record, 'tumor_stage', 'T1') == set()
    assert not Measurement.objects.filter(person=record.person).exists()
    refreshed = refresh_patient_record(record.person)
    assert refreshed.tumor_stage == 'T1'
    assert 'tumor_stage' in refreshed.user_edited_fields


def test_scoped_coded_answer_and_question_override_survive_refresh(record):
    choice = FieldChoice.objects.create(field_name='tumor_stage', context_key='BC:p',
        display='Tumor category one', canonical_value='T1')
    answer = ConceptFactory(domain__domain_id='Meas Value')
    question = ConceptFactory()
    save_mapping(choice, {'status': 'approved', 'outcome': 'mapped', 'target_concept': answer,
        'question_concept': question, 'notes': 'Reviewed pathological staging scope.', 'vocabulary_release': 'Test release'})
    assert write(record, 'tumor_stage', 'T1') == {'tumor_stage'}
    row = Measurement.objects.get(person=record.person)
    assert row.measurement_concept_id == question.pk
    assert row.value_as_concept_id == answer.pk
    Measurement.objects.filter(pk=row.pk).update(value_as_string=None)
    refreshed = refresh_patient_record(record.person)
    assert refreshed.tumor_stage == 'T1'
    assert refreshed.staging_modalities == 'p'
    assert write(record, 'tumor_stage', None) == {'tumor_stage'}
    row.refresh_from_db()
    assert row.value_source_value == CLEAR_VALUE
    assert refresh_patient_record(record.person).tumor_stage is None


def test_descriptor_exposes_scoped_options_and_basis_without_a_fake_question(record):
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    FieldChoice.objects.create(field_name='tumor_stage', context_key='BC:p',
        display='Pathological category one', canonical_value='T1')
    descriptor = build_writable_field_descriptor()
    assert descriptor['tumor_stage']['options_by_context']['BC:p'][0]['value'] == 'T1'
    assert descriptor['staging_modalities']['writable']
    assert 'projection' not in descriptor['staging_modalities']
    assert [o['value'] for o in descriptor['staging_modalities']['options']] == ['c', 'p', 'yp']


def test_api_uses_basis_from_the_same_patch_and_retry_is_idempotent(record):
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='staging-editor@example.test', is_staff=True))
    payload = {'staging_modalities': 'yp', 'tumor_stage': 'T1'}
    response = client.patch(f'/api/v1/patient-records/{record.person_id}/', payload, format='json')
    assert response.status_code == 200, response.data
    assert 'tumor_stage' not in response.data['user_edited_fields']
    row = Measurement.objects.get(person=record.person)
    assert row.measurement_source_value == '21899-0'
    assert row.qualifier_source_value == 'yp'
    moment = row.measurement_datetime
    response = client.patch(f'/api/v1/patient-records/{record.person_id}/', payload, format='json')
    assert response.status_code == 200
    assert Measurement.objects.filter(person=record.person).count() == 1
    row.refresh_from_db()
    assert row.measurement_datetime == moment


def test_scalar_edit_preserves_linked_assessment(record):
    from tests.factories import MeasurementFactory
    linked = MeasurementFactory(person=record.person, measurement_source_value='21899-0',
        measurement_concept=ConceptFactory(concept_code='21899-0'),
        measurement_date=timezone.localdate(), value_as_string='T4',
        measurement_event_id=123, meas_event_field_concept=ConceptFactory())
    assert write(record, 'tumor_stage', 'T1') == {'tumor_stage'}
    linked.refresh_from_db()
    assert linked.value_as_string == 'T4'
    assert linked.measurement_event_id == 123
    assert refresh_patient_record(record.person).tumor_stage == 'T1'


def test_scoped_approval_rejects_a_question_for_the_wrong_basis(record):
    from django.core.exceptions import ValidationError
    choice = FieldChoice.objects.create(field_name='tumor_stage', context_key='BC:p', display='T1')
    with pytest.raises(ValidationError, match='staging basis'):
        save_mapping(choice, {'status': 'approved', 'outcome': 'mapped',
            'target_concept': ConceptFactory(domain__domain_id='Meas Value'),
            'question_concept': FieldConceptMapping.objects.get(field_name='tumor_stage').concept,
            'notes': 'Conflicting clinical question and pathological scope.', 'vocabulary_release': 'Test release'})


def test_unknown_question_cannot_silently_ignore_selected_basis(record):
    question = ConceptFactory()
    FieldConceptMapping.objects.filter(field_name='tumor_stage').update(
        concept=question, concept_code=question.concept_code, source_value=question.concept_code)
    assert write(record, 'tumor_stage', 'T1') == set()
    assert not Measurement.objects.filter(person=record.person).exists()
