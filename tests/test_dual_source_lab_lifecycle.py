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
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, ProvenanceRecord
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
    # The pair is a dated source pair (#2397): a supplied draw date lands on
    # the projected result, a plain edit lands today.
    data = {'hemoglobin_level': value, 'hemoglobin_level_units': unit}
    if when is not None:
        data['hemoglobin_level'] = {'value': value, 'date': when.isoformat()}
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
            imported.measurement_type_concept_id,
            imported.measurement_date, imported.measurement_source_value) == (
        10, 'g/dL', EHR_TYPE, TODAY, LOINC)
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert (patient.value_as_number, patient.unit_source_value) == (12, 'g/dL')
    assert patient.measurement_date == TODAY
    provenance = ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(Measurement),
        object_id=patient.pk, source='PATIENT_SELF',
    ).values_list('source_user_id', flat=True)
    assert list(provenance) == [f"{lab['identity'].issuer}|{lab['identity'].sub}"]


def test_an_older_import_does_not_erase_a_newer_patient_result(lab):
    _patch_pair(lab, 120, 'G/L', TODAY)

    _import(lab, 10, 'g/dL', TODAY - timedelta(days=1))

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_g_dl == Decimal('12.0')


def test_a_later_same_day_import_does_not_take_over_the_patient_answer(lab):
    # The pair is still pending, and refresh restores it before recompute. This
    # is the documented source-pair retention, not a general same-day tie rule.
    _import(lab, 10, 'g/dL', TODAY)
    _patch_pair(lab, 120, 'G/L', TODAY)
    _import(lab, 11, 'g/dL', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert patient.value_as_number == 12


def test_a_failed_lab_mapping_keeps_the_answer_pending_then_projects_once(lab):
    # Remove the LOINC concept so the canonical has no projection target. The
    # pair is then undated too: there is no result row to carry the date.
    lab['concept'].delete()
    _patch_pair(lab, 120, 'G/L')

    record = lab['record']
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert not _hemoglobin_rows(lab, SELF_REPORT_TYPE).exists()
    assert 'hemoglobin_level' in (record.user_edited_fields or [])

    # Mapping availability plus an explicit retry: the same answer projects
    # once, beside nothing.
    ConceptFactory(
        concept_id=900718, concept_code=LOINC,
        concept_name='Hemoglobin [Mass/volume] in Blood',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id='Measurement'),
        concept_class=ConceptClassFactory(concept_class_id='Lab Test'),
    )
    _patch_pair(lab, 120, 'G/L')

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


@pytest.mark.parametrize(
    'representation,value,unit,expected_canonical,expected_pair,expected_unit',
    [
        ('loinc', 12.0, 'g/dL', Decimal('12.0'), Decimal('12.00'), 'G/DL'),
        ('loinc', 120, 'g/L', Decimal('12.0'), Decimal('12.00'), 'G/DL'),
        ('display', 120, 'g/L', Decimal('12.0'), Decimal('12.00'), 'G/DL'),
        ('legacy', 120, 'g/L', Decimal('12.0'), Decimal('120.00'), 'G/L'),
        ('loinc', 12.0, None, Decimal('12.0'), Decimal('12.00'), 'G/DL'),
    ],
)
def test_the_hemoglobin_import_unit_matrix(
    lab, representation, value, unit, expected_canonical, expected_pair,
    expected_unit,
):
    concept = lab['concept']
    kwargs = {
        'person': lab['person'], 'value_as_number': value,
        'unit_source_value': unit, 'measurement_type_concept_id': EHR_TYPE,
    }
    if representation == 'loinc':
        kwargs.update(measurement_concept=concept, measurement_source_value=LOINC)
    elif representation == 'display':
        kwargs.update(measurement_concept_id=0,
                      measurement_source_value='Hemoglobin [Mass/volume] in Blood')
    else:
        kwargs.update(measurement_concept=ConceptFactory(concept_name='hemoglobin'),
                      measurement_source_value=None)
    with suppress_patient_record_refresh():
        MeasurementFactory(**kwargs)
    refresh_patient_record(lab['person'])

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == expected_canonical
    assert record.hemoglobin_level == expected_pair
    assert record.hemoglobin_level_units == expected_unit


def test_a_ucum_unit_concept_is_used_when_the_source_text_is_absent(lab):
    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=lab['person'], measurement_concept=lab['concept'],
            measurement_source_value=LOINC, measurement_type_concept_id=EHR_TYPE,
            value_as_number=120,
            unit_concept=ConceptFactory(
                vocabulary=VocabularyFactory(vocabulary_id='UCUM'),
                concept_code='g/L'),
        )
    refresh_patient_record(lab['person'])

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')


def test_a_newer_unsupported_unit_blocks_the_older_convertible_result(lab):
    _import(lab, 120, 'g/L', TODAY - timedelta(days=1))
    _import(lab, 120, 'mg/L', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl is None
    assert record.hemoglobin_level is None
    assert record.hemoglobin_level_units is None


def test_an_out_of_range_import_leaves_no_canonical_read_model(lab):
    imported = _import(lab, 100000, 'g/L', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl is None
    # The OMOP fact is still the import's own record; only the canonical
    # read-model refuses a value the column cannot hold.
    imported.refresh_from_db()
    assert imported.value_as_number == 100000


def test_a_dated_pair_keeps_the_draw_date_through_the_refresh(lab):
    draw = TODAY - timedelta(days=3)
    _patch_pair(lab, 120, 'G/L', draw)

    record = lab['record']
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_level_units == 'G/L'
    assert record.hemoglobin_g_dl == Decimal('12.0')
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert patient.measurement_date == draw

    # A full refresh restores the pending pair and keeps the same draw date,
    # rather than re-dating the answer to the refresh day.
    refresh_patient_record(lab['person'])
    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert _hemoglobin_rows(lab, SELF_REPORT_TYPE).get().measurement_date == draw


def test_a_newer_dated_import_retires_the_patient_pair(lab):
    draw = TODAY - timedelta(days=2)
    _patch_pair(lab, 120, 'G/L', draw)

    record = lab['record']
    record.refresh_from_db()
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': draw.isoformat()}

    _import(lab, 8.0, 'g/dL', TODAY)

    record.refresh_from_db()
    # The newer result is now current, and the older pair no longer hides it.
    assert record.hemoglobin_g_dl == Decimal('8.0')
    assert record.hemoglobin_level == Decimal('8.00')
    assert record.hemoglobin_level_units == 'G/DL'
    assert not {'hemoglobin_level', 'hemoglobin_level_units'} & set(
        record.user_edited_fields or [])
    assert record.pending_source_pair_dates == {}
    # The patient's own fact stays history; retirement does not delete it.
    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert patient.measurement_date == draw


def test_a_same_day_import_keeps_the_pair_and_its_day(lab):
    _patch_pair(lab, 120, 'G/L', TODAY)

    _import(lab, 11, 'g/dL', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': TODAY.isoformat()}


def test_a_legacy_pair_without_a_day_is_not_retired_by_a_newer_import(lab):
    record = lab['record']
    record.hemoglobin_level = Decimal('120')
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = Decimal('12.0')
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    _import(lab, 8.0, 'g/dL', TODAY)

    record.refresh_from_db()
    # No day was ever recorded, so the record does not guess one.
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert record.hemoglobin_level == Decimal('120.00')
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])
    assert record.pending_source_pair_dates == {}


def test_a_newer_unusable_result_keeps_the_pair_pending(lab):
    draw = TODAY - timedelta(days=2)
    _patch_pair(lab, 120, 'G/L', draw)

    # A newer result in a unit this slice cannot read owns the field, but it
    # cannot replace the patient's answer: derivation selects no value for it.
    _import(lab, 120, 'mg/L', TODAY)

    record = lab['record']
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('12.0')
    assert record.hemoglobin_level == Decimal('120.00')
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': draw.isoformat()}


def test_a_failed_projection_keeps_the_day_and_a_retry_uses_it(lab):
    # The mapping resolves, so the pair is datable, but the projection itself
    # cannot write a fact without its type concept.
    from omop_core.models import Concept
    Concept.objects.filter(concept_id__in=(EHR_TYPE, SELF_REPORT_TYPE)).delete()
    draw = TODAY - timedelta(days=5)
    _patch_pair(lab, 120, 'G/L', draw)

    record = lab['record']
    assert 'hemoglobin_level' in (record.user_edited_fields or [])
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': draw.isoformat()}
    assert not _hemoglobin_rows(lab, SELF_REPORT_TYPE).exists()

    ConceptFactory(concept_id=EHR_TYPE, concept_code='ehr-type')
    ConceptFactory(concept_id=SELF_REPORT_TYPE, concept_code='patient-report-type')
    # The retry is undated; it must land on the day first entered, not today.
    _patch_pair(lab, 120, 'G/L')

    patient = _hemoglobin_rows(lab, SELF_REPORT_TYPE).get()
    assert patient.measurement_date == draw
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': draw.isoformat()}


def test_an_undated_echo_does_not_move_the_pair_day(lab):
    draw = TODAY - timedelta(days=4)
    _patch_pair(lab, 120, 'G/L', draw)

    # The shared form resends the whole record; an unchanged pair is a retry,
    # not a new result dated today.
    _patch_pair(lab, 120, 'G/L')

    record = lab['record']
    record.refresh_from_db()
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': draw.isoformat()}
    assert _hemoglobin_rows(lab, SELF_REPORT_TYPE).get().measurement_date == draw


def test_the_pair_day_is_server_managed_and_not_exposed(lab):
    record = lab['record']
    record.pending_source_pair_dates = {'hemoglobin_level': TODAY.isoformat()}
    record.save()

    response = lab['client'].get(
        f"/api/v1/patient-records/{lab['person'].person_id}/")
    assert response.status_code == 200
    assert 'pending_source_pair_dates' not in response.data

    response = lab['client'].patch(
        f"/api/v1/patient-records/{lab['person'].person_id}/",
        {'pending_source_pair_dates': {'hemoglobin_level': '2020-01-01'}},
        format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': TODAY.isoformat()}


def test_a_changed_pair_after_a_dated_answer_is_dated_today(lab):
    draw = TODAY - timedelta(days=6)
    _patch_pair(lab, 120, 'G/L', draw)

    # A new number without a date is a new result, not a retry of the old one.
    _patch_pair(lab, 130, 'G/L')

    record = lab['record']
    record.refresh_from_db()
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': TODAY.isoformat()}
    rows = _hemoglobin_rows(lab, SELF_REPORT_TYPE).order_by('measurement_date')
    assert [row.measurement_date for row in rows] == [draw, TODAY]
    assert rows.last().value_as_number == 13


def test_a_changed_unit_after_a_dated_answer_is_dated_today(lab):
    draw = TODAY - timedelta(days=7)
    _patch_pair(lab, 12, 'G/DL', draw)

    # The number is unchanged; the unit the reader chose is not, so the pair is
    # a new assertion and must not inherit the old draw date.
    _patch_pair(lab, 12, 'G/L')

    record = lab['record']
    record.refresh_from_db()
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': TODAY.isoformat()}
    rows = _hemoglobin_rows(lab, SELF_REPORT_TYPE).order_by('measurement_date')
    assert [row.measurement_date for row in rows] == [draw, TODAY]


def test_an_echo_of_a_legacy_pair_does_not_invent_a_day(lab):
    record = lab['record']
    record.hemoglobin_level = Decimal('120')
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = Decimal('12.0')
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    _patch_pair(lab, 120, 'G/L')

    record.refresh_from_db()
    # The resend is an echo; the record never knew the day, so it records none.
    assert record.pending_source_pair_dates == {}
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])


def test_a_clear_of_a_dated_pair_is_dated_today(lab):
    draw = TODAY - timedelta(days=8)
    _patch_pair(lab, 120, 'G/L', draw)

    _patch_pair(lab, None, 'G/L')

    record = lab['record']
    record.refresh_from_db()
    assert record.pending_source_pair_dates == {
        'hemoglobin_level': TODAY.isoformat()}
    assert record.hemoglobin_g_dl is None
