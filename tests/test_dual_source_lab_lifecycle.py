"""Imported and patient-entered lab values keep their own histories (#2136).

The FHIR/import path writes an EHR-typed Measurement and derives PatientRecord
from it. A later patient answer must save on the record first, project as a
patient self-report fact beside the import, and never take over the imported
value, date, source or provenance. A failed projection keeps the answer
pending; retrying lands it once. An older import must not erase a newer answer.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, MeasurementFactory,
    PatientRecordFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

EHR_TYPE = 32817
SELF_REPORT_TYPE = 32865
LOINC = '718-7'
TODAY = timezone.localdate()


@pytest.fixture
def lab(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR_TYPE, concept_code='ehr-type')
    ConceptFactory(concept_id=SELF_REPORT_TYPE, concept_code='patient-report-type')
    ConceptFactory(concept_code='g/dL', vocabulary=VocabularyFactory(vocabulary_id='UCUM'))
    hemoglobin = ConceptFactory(
        concept_id=900718, concept_code=LOINC,
        concept_name='Hemoglobin [Mass/volume] in Blood',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id='Measurement'),
        concept_class=ConceptClassFactory(concept_class_id='Lab Test'),
    )
    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    identity = Identity.objects.create_user(email='dual-source@example.test')
    PatientUser.objects.create(identity=identity, person=person)
    client = APIClient()
    client.force_authenticate(identity)
    return {'person': person, 'record': record, 'identity': identity,
            'client': client, 'concept': hemoglobin}


def _import(lab, value, unit, when, type_id=EHR_TYPE):
    with suppress_patient_record_refresh():
        row = MeasurementFactory(
            person=lab['person'],
            measurement_concept=lab['concept'],
            measurement_source_value=LOINC,
            measurement_date=when,
            measurement_type_concept_id=type_id,
            value_as_number=value,
            unit_source_value=unit,
        )
    refresh_patient_record(lab['person'])
    return row


def _patch_pair(lab, value, unit, when=None):
    # The pair descriptor is not dated yet; a plain edit lands today. `when`
    # documents the clinical day the test is reasoning about.
    data = {'hemoglobin_level': value, 'hemoglobin_level_units': unit}
    response = lab['client'].patch(
        f"/api/v1/patient-records/{lab['person'].person_id}/", data,
        format='json', HTTP_X_PATIENT_FORM='1',
    )
    assert response.status_code == 200, response.data
    lab['record'].refresh_from_db()
    return response


def _hemoglobin_rows(lab, type_id=None):
    rows = Measurement.objects.filter(
        person=lab['person'], measurement_concept__concept_code=LOINC)
    if type_id is not None:
        rows = rows.filter(measurement_type_concept_id=type_id)
    return rows.order_by('measurement_date', 'measurement_id')


def test_an_imported_result_reads_back_with_its_source_and_unit(lab):
    imported = _import(lab, 120, 'g/L', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert record.hemoglobin_level == Decimal('12.00')
    assert record.hemoglobin_level_units == 'G/DL'
    imported.refresh_from_db()
    assert (imported.value_as_number, imported.unit_source_value) == (120, 'g/L')
    assert imported.measurement_type_concept_id == EHR_TYPE
    assert imported.measurement_source_value == LOINC


def test_a_patient_result_sits_beside_the_imported_fact(lab):
    imported = _import(lab, 10, 'g/dL', TODAY)

    _patch_pair(lab, 120, 'G/L', TODAY)

    record = lab['record']
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_level_units == 'G/L'
    assert record.hemoglobin_g_dl == Decimal('12.0')

    imported.refresh_from_db()
    assert (imported.value_as_number, imported.unit_source_value,
            imported.measurement_type_concept_id) == (10, 'g/dL', EHR_TYPE)
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert (patient.value_as_number, patient.unit_source_value) == (12, 'g/dL')
    assert patient.measurement_date == TODAY


def test_an_older_import_does_not_erase_a_newer_patient_result(lab):
    _patch_pair(lab, 120, 'G/L', TODAY)

    _import(lab, 10, 'g/dL', TODAY - timedelta(days=1))

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_g_dl == Decimal('12.0')


def test_a_later_same_day_import_does_not_take_over_the_patient_answer(lab):
    _import(lab, 10, 'g/dL', TODAY)
    _patch_pair(lab, 120, 'G/L', TODAY)
    _import(lab, 11, 'g/dL', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert patient.value_as_number == 12


def test_a_failed_lab_mapping_keeps_the_answer_pending_then_projects_once(lab):
    # Remove the LOINC concept so the canonical has no projection target.
    lab['concept'].delete()
    _patch_pair(lab, 120, 'G/L', TODAY)

    record = lab['record']
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert not _hemoglobin_rows(lab, SELF_REPORT_TYPE).exists()
    assert 'hemoglobin_level' in (record.user_edited_fields or [])

    # Later mapping approval: the same answer projects once, beside nothing.
    ConceptFactory(
        concept_id=900718, concept_code=LOINC,
        concept_name='Hemoglobin [Mass/volume] in Blood',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id='Measurement'),
        concept_class=ConceptClassFactory(concept_class_id='Lab Test'),
    )
    _patch_pair(lab, 120, 'G/L', TODAY)

    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE)
    assert patient.count() == 1
    assert patient.get().value_as_number == 12


def test_repeated_patient_edits_reuse_one_self_report_fact(lab):
    _import(lab, 10, 'g/dL', TODAY)
    _patch_pair(lab, 120, 'G/L', TODAY)
    _patch_pair(lab, 130, 'G/L', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('13.0')
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE)
    assert patient.count() == 1
    assert patient.get().value_as_number == 13
    assert _hemoglobin_rows(lab, EHR_TYPE).count() == 1


def test_computed_only_verdicts_stay_refused_while_lab_inputs_are_writable(lab):
    for field, value in (
        ('bmi', 99), ('meets_crab', True), ('meets_slim', True),
    ):
        response = lab['client'].patch(
            f"/api/v1/patient-records/{lab['person'].person_id}/",
            {field: value}, format='json', HTTP_X_PATIENT_FORM='1',
        )
        assert response.status_code == 400, (field, response.data)
        assert field in response.data['rejected']

    # The paired positive control: the lab source input is accepted.
    _patch_pair(lab, 120, 'G/L')
    assert lab['record'].hemoglobin_g_dl == Decimal('12.0')
