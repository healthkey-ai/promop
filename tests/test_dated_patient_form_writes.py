"""#5608: a lab result keeps its clinical date through the shared form.

`apply_patient_form_patch` used to stamp every fact with the day it was typed.
These tests anchor the dated envelope to the derivation: a supplied date is the
measurement date, a correction at the latest date updates that row, and an older
result is stored as history rather than promoted to the current value
(docs/clinical-event-time-policy.md).
"""
from datetime import date, timedelta

import pytest

from omop_core.models import Concept, Measurement, PatientRecord
from omop_core.services.patient_form import apply_patient_form_patch, patient_form_descriptors
from omop_core.services.pk import next_pk
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

FIELD = 'hemoglobin_g_dl'
LOINC = '718-7'


def _person_with_lab():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code=LOINC, concept_name='Hemoglobin [Mass/volume] in Blood')
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=32865, concept_code='PT-SELF-REPORT',
                   concept_name='Patient self-report', vocabulary=type_vocab)
    return person


def _fact(person):
    return Measurement.objects.get(person=person, measurement_concept_id__isnull=False)


def test_the_base_descriptor_marks_a_scalar_fact_as_dated():
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    _person_with_lab()

    assert build_writable_field_descriptor()[FIELD]['dated'] is True


def test_a_dedicated_clinical_input_is_not_dated():
    from omop_core.services.clinical_input_contract import CLINICAL_INPUT_FIELDS
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    _person_with_lab()
    descriptor = build_writable_field_descriptor()

    marked = {name for name in CLINICAL_INPUT_FIELDS
              if descriptor.get(name, {}).get('dated')}
    assert not marked, f'dedicated writers must not take a caller-supplied date: {marked}'


def test_the_descriptor_names_the_latest_recorded_date_for_a_dated_lab():
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=12.5)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['dated'] is True
    assert entry['latest_date'] == '2026-08-01'


def test_a_newer_fact_without_a_value_does_not_move_the_correction_date():
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    numeric = Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 7, 1),
        measurement_type_concept_id=32865, value_as_number=12.5)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=None)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['latest_date'] == numeric.measurement_date.isoformat()


def test_the_date_follows_a_legacy_named_fact_that_supplies_the_value():
    from tests.factories import ConceptFactory

    person = _person_with_lab()
    legacy = ConceptFactory(concept_code='HGB', concept_name='Hemoglobin').concept_id
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept_id=legacy, measurement_date=date(2026, 7, 5),
        measurement_type_concept_id=32865, value_as_number=12.0,
        unit_source_value='g/dL')
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['latest_date'] == '2026-07-05'


def test_a_restricted_readable_set_does_not_name_an_excluded_count():
    from tests.factories import ConceptFactory

    person = _person_with_lab()
    ConceptFactory(concept_code='751-8', concept_name='Absolute neutrophil count')
    ConceptFactory(concept_code='777-3', concept_name='Platelet count')
    for code, value in (('751-8', 4.2), ('777-3', 150)):
        Measurement.objects.create(
            measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
            measurement_concept=Concept.objects.get(concept_code=code),
            measurement_date=date(2026, 8, 1), measurement_type_concept_id=32865,
            value_as_number=value)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(
        readable_fields={'anc_thousand_per_ul'}, record=record)['anc_thousand_per_ul']

    assert entry['latest_date'] == '2026-08-01'


def test_a_dated_edit_records_the_supplied_clinical_date():
    person = _person_with_lab()

    record, result = apply_patient_form_patch(
        person, {FIELD: {'value': 12.5, 'date': '2026-07-04'}}, allowed_fields={FIELD})

    assert FIELD in result.applied
    fact = _fact(person)
    assert fact.measurement_date == date(2026, 7, 4)
    assert fact.measurement_datetime is None
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(12.5)


def test_a_future_date_is_refused_before_anything_is_written():
    person = _person_with_lab()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match='future'):
        apply_patient_form_patch(
            person, {FIELD: {'value': 12.5, 'date': tomorrow}}, allowed_fields={FIELD})

    assert not Measurement.objects.filter(person=person).exists()


@pytest.mark.parametrize('payload', [
    {'value': 12.5},
    {'value': 12.5, 'date': '2026-13-01'},
    {'value': 12.5, 'date': '2026-07-04', 'extra': True},
])
def test_a_malformed_dated_envelope_is_refused(payload):
    person = _person_with_lab()

    with pytest.raises(ValueError):
        apply_patient_form_patch(person, {FIELD: payload}, allowed_fields={FIELD})

    assert not Measurement.objects.filter(person=person).exists()


def test_a_correction_at_the_same_date_updates_that_result():
    person = _person_with_lab()
    apply_patient_form_patch(
        person, {FIELD: {'value': 12.5, 'date': '2026-07-04'}}, allowed_fields={FIELD})
    apply_patient_form_patch(
        person, {FIELD: {'value': 13.0, 'date': '2026-07-04'}}, allowed_fields={FIELD})

    assert Measurement.objects.filter(person=person).count() == 1
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(13.0)


def test_an_older_result_is_history_not_the_current_value():
    person = _person_with_lab()
    apply_patient_form_patch(
        person, {FIELD: {'value': 12.0, 'date': '2026-08-01'}}, allowed_fields={FIELD})
    apply_patient_form_patch(
        person, {FIELD: {'value': 14.0, 'date': '2026-07-01'}}, allowed_fields={FIELD})

    dates = sorted(Measurement.objects.filter(person=person).values_list(
        'measurement_date', flat=True))
    assert dates == [date(2026, 7, 1), date(2026, 8, 1)]
    # The later result still wins derivation even though the older row was edited last.
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(12.0)


def test_an_undated_patch_keeps_todays_date_for_compatibility():
    person = _person_with_lab()

    apply_patient_form_patch(person, {FIELD: 12.5}, allowed_fields={FIELD})

    assert _fact(person).measurement_date == date.today()


def test_a_non_dated_field_refuses_a_result_date():
    person = _person_with_lab()

    with pytest.raises(ValueError, match='does not accept a result date'):
        apply_patient_form_patch(
            person, {'systolic_blood_pressure': {'value': 120, 'date': '2026-07-04'}},
            allowed_fields={'systolic_blood_pressure'})
