"""Shared form stores mapped scalars on PatientRecord before projecting them.

The shared form is the federation patient editor (`X-Patient-Form: 1`). Mapped
scalars used to be written through the legacy OMOP-first applier, which called
`refresh_patient_record()` and lost the edit when projection failed. These tests
pin the PatientRecord-first contract from docs/patient-record-first-writes.md.
"""
import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, FieldConceptMapping, Measurement, Observation, PatientRecord,
    ProvenanceRecord,
)
from omop_core.services.patient_form import apply_patient_form_patch
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

LOINC_VOCAB = 'LOINC'
UCUM_VOCAB = 'UCUM'
TYPE_VOCAB = 'Type Concept'
HEMOGLOBIN_CONCEPT = 3000963
HEMOGLOBIN_CODE = '718-7'
PATIENT_SELF_REPORT = 32865
EHR_TYPE = 32817
FEVER_CONCEPT = 386661006
FEVER_CODE = '386661006'


def _ensure_concept(concept_id, concept_code, vocabulary_id, concept_name,
                    domain_id=None):
    concept = Concept.objects.filter(pk=concept_id).first()
    if concept is not None:
        return concept
    domain = DomainFactory(domain_id=domain_id) if domain_id else None
    return ConceptFactory(
        concept_id=concept_id, concept_code=concept_code,
        vocabulary_id=vocabulary_id, concept_name=concept_name, domain=domain,
    )


def _seed_lab_vocab():
    for vocabulary_id in (LOINC_VOCAB, UCUM_VOCAB, TYPE_VOCAB, 'SNOMED'):
        if not Concept.objects.filter(vocabulary_id=vocabulary_id).exists():
            VocabularyFactory(vocabulary_id=vocabulary_id)
    _ensure_concept(
        HEMOGLOBIN_CONCEPT, HEMOGLOBIN_CODE, LOINC_VOCAB, 'Hemoglobin',
        domain_id='Measurement',
    )
    _ensure_concept(8713, 'g/dL', UCUM_VOCAB, 'gram per deciliter', domain_id='Unit')
    _ensure_concept(
        PATIENT_SELF_REPORT, str(PATIENT_SELF_REPORT), TYPE_VOCAB,
        'Patient self-report', domain_id='Type Concept',
    )
    _ensure_concept(EHR_TYPE, str(EHR_TYPE), TYPE_VOCAB, 'EHR', domain_id='Type Concept')


def _curated_fever_mapping():
    _seed_lab_vocab()
    concept = _ensure_concept(
        FEVER_CONCEPT, FEVER_CODE, 'SNOMED', 'Fever', domain_id='Observation',
    )
    return FieldConceptMapping.objects.create(
        field_name='b_symptom_fever',
        concept=concept,
        vocabulary_id='SNOMED',
        concept_code=FEVER_CODE,
        omop_table='observation',
        source_value=FEVER_CODE,
        value_kind='boolean',
        type_concept_id=EHR_TYPE,
        status='approved',
    )


def _staff_client():
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='shared-form-staff@example.test', is_staff=True)
    )
    return client


def _patient_client(person):
    identity = Identity.objects.create_user(email='shared-form-patient@example.test')
    PatientUser.objects.create(identity=identity, person=person)
    client = APIClient()
    client.force_authenticate(identity)
    return client


def _url(record):
    return f'/api/v1/patient-records/{record.person_id}/'


def _patch(client, record, payload):
    return client.patch(_url(record), payload, format='json', HTTP_X_PATIENT_FORM='1')


def test_staff_scalar_saves_to_patient_record_and_keeps_recipe_type():
    _curated_fever_mapping()
    record = PatientRecordFactory()

    response = _patch(_staff_client(), record, {'b_symptom_fever': True})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.b_symptom_fever is True
    fact = Observation.objects.get(
        person=record.person, observation_concept_id=FEVER_CONCEPT,
        observation_source_value=FEVER_CODE,
    )
    assert fact.observation_type_concept_id == EHR_TYPE
    assert not ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(Observation),
        object_id=fact.pk, source='PATIENT_SELF',
    ).exists()


def test_patient_scalar_is_self_report_with_provenance():
    _curated_fever_mapping()
    record = PatientRecordFactory()

    response = _patch(_patient_client(record.person), record, {'b_symptom_fever': True})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.b_symptom_fever is True
    fact = Observation.objects.get(
        person=record.person, observation_concept_id=FEVER_CONCEPT,
        observation_source_value=FEVER_CODE,
    )
    assert fact.observation_type_concept_id == PATIENT_SELF_REPORT
    assert ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(Observation),
        object_id=fact.pk, source='PATIENT_SELF',
    ).exists()


def test_projection_failure_keeps_the_saved_answer(monkeypatch):
    _seed_lab_vocab()
    record = PatientRecordFactory()
    observed = {}

    def boom(person, field, value, projection, **_kwargs):
        stored = PatientRecord.objects.get(person=person)
        observed['value'] = stored.hemoglobin_g_dl
        observed['pending'] = list(stored.user_edited_fields or [])
        raise RuntimeError('projection is down')

    monkeypatch.setattr('omop_core.services.omop_projection.project_single_value', boom)

    response = _patch(_staff_client(), record, {'hemoglobin_g_dl': 9.5})

    assert response.status_code == 200, response.data
    # The projector runs after the record write: the answer and its pending
    # marker must already be visible inside the failing projection.
    assert observed['value'] == 9.5
    assert 'hemoglobin_g_dl' in observed['pending']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == 9.5
    assert 'hemoglobin_g_dl' in (record.user_edited_fields or [])
    assert not Measurement.objects.filter(
        person=record.person, measurement_concept_id=HEMOGLOBIN_CONCEPT,
    ).exists()
    assert 'hemoglobin_g_dl' in response.data['write']['applied']


def test_clear_and_same_day_correction_remove_the_tombstone():
    _seed_lab_vocab()
    record = PatientRecordFactory()
    client = _staff_client()

    assert _patch(client, record, {'hemoglobin_g_dl': 9.5}).status_code == 200
    assert _patch(client, record, {'hemoglobin_g_dl': None}).status_code == 200
    fact = Measurement.objects.get(
        person=record.person, measurement_concept_id=HEMOGLOBIN_CONCEPT,
    )
    assert fact.value_source_value == 'PatientRecord:cleared'

    assert _patch(client, record, {'hemoglobin_g_dl': 10.5}).status_code == 200
    fact.refresh_from_db()
    assert fact.value_as_number == 10.5
    assert fact.value_source_value is None
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == 10.5


def test_a_different_source_row_is_not_rewritten():
    _seed_lab_vocab()
    record = PatientRecordFactory()
    other = MeasurementFactory(
        person=record.person,
        measurement_concept=Concept.objects.get(pk=HEMOGLOBIN_CONCEPT),
        measurement_date=timezone.localdate(),
        measurement_type_concept=Concept.objects.get(pk=PATIENT_SELF_REPORT),
        measurement_source_value='OTHER-SOURCE',
        value_as_number=7.0,
    )

    response = _patch(_staff_client(), record, {'hemoglobin_g_dl': 9.5})

    assert response.status_code == 200, response.data
    other.refresh_from_db()
    assert other.value_as_number == 7.0
    assert other.measurement_source_value == 'OTHER-SOURCE'
    assert Measurement.objects.filter(
        person=record.person, measurement_concept_id=HEMOGLOBIN_CONCEPT,
        measurement_source_value=HEMOGLOBIN_CODE,
    ).count() == 1


def test_scalar_save_does_not_call_refresh(monkeypatch):
    _seed_lab_vocab()
    record = PatientRecordFactory()
    calls = []
    monkeypatch.setattr(
        'omop_core.services.patient_form.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )

    response = _patch(_staff_client(), record, {'hemoglobin_g_dl': 9.5})

    assert response.status_code == 200, response.data
    assert calls == []


def test_staff_does_not_overwrite_a_patient_self_report_fact():
    _curated_fever_mapping()
    record = PatientRecordFactory()

    assert _patch(_patient_client(record.person), record, {'b_symptom_fever': True}).status_code == 200
    patient_fact = Observation.objects.get(
        person=record.person, observation_concept_id=FEVER_CONCEPT,
        observation_type_concept_id=PATIENT_SELF_REPORT,
    )

    assert _patch(_staff_client(), record, {'b_symptom_fever': False}).status_code == 200

    patient_fact.refresh_from_db()
    assert patient_fact.observation_type_concept_id == PATIENT_SELF_REPORT
    assert patient_fact.value_as_number == 1.0
    clinical = Observation.objects.get(
        person=record.person, observation_concept_id=FEVER_CONCEPT,
        observation_type_concept_id=EHR_TYPE,
    )
    assert clinical.value_as_number == 0.0


def test_patient_scalar_without_self_report_type_stays_pending(monkeypatch):
    _curated_fever_mapping()
    record = PatientRecordFactory()
    monkeypatch.setattr(
        'omop_core.services.omop_projection.patient_self_report_type', lambda: None,
    )

    response = _patch(_patient_client(record.person), record, {'b_symptom_fever': True})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.b_symptom_fever is True
    assert 'b_symptom_fever' in (record.user_edited_fields or [])
    assert not Observation.objects.filter(
        person=record.person, observation_concept_id=FEVER_CONCEPT,
    ).exists()


def test_scalar_string_limit_boundary():
    _seed_lab_vocab()
    record = PatientRecordFactory()
    recipe = {
        'progression': {
            'kind': 'direct', 'writable': True, 'target': 'patient_record',
            'value_kind': 'string',
            'projection': {'omop_table': 'observation', 'concept_id': FEVER_CONCEPT},
        },
    }

    saved, result = apply_patient_form_patch(
        record.person, {'progression': 'a' * 60}, descriptor=recipe,
    )
    assert 'progression' in result.applied
    assert saved.progression == 'a' * 60

    with pytest.raises(ValueError, match='at most 60 characters'):
        apply_patient_form_patch(
            record.person, {'progression': 'a' * 61}, descriptor=recipe,
        )
