"""Calculated fields use provisional inputs (#2661).

With the switch on, a calculated field (BMI, receptor and renal status, B symptoms)
whose inputs include a proposal is itself a proposal, at the lowest confidence of the
inputs that are proposals. patient_record never holds one: the calculation runs over
a copy, a real answer beats it, and an approval moves it to the real record.
"""
from datetime import date

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    FieldFormula, Measurement, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
)
from omop_core.services import provisional_record
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity
from tests.factories import MeasurementFactory
from tests.test_provisional_derivation import (  # noqa: F401 (fixtures)
    _add_observation, _concept, record, snomed,
)

pytestmark = pytest.mark.django_db

WEIGHT, HEIGHT = '29463-7', '8302-2'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True


def _vital(person, code, value, unit, conf, when=date(2024, 6, 1)):
    concept = _concept('Measurement', 3000000 + int(code.split('-')[0]), code, f'Vital {code}', 'LOINC')
    return MeasurementFactory(
        person=person, measurement_concept=concept, measurement_date=when,
        value_as_number=value, measurement_source_value=code, unit_source_value=unit,
        value_source_value=str(value), confidence=conf)


def _weight(person, value=70, conf=None, unit='kg'):
    return _vital(person, WEIGHT, value, unit, conf)


def _height(person, value=170, conf=None, unit='cm'):
    return _vital(person, HEIGHT, value, unit, conf)


def pair(person):
    return (PatientRecordProvisional.objects.get(person=person),
            PatientRecordConfidence.objects.get(person=person))


def _refresh(person):
    return refresh_patient_record(person)


def test_a_bmi_is_worked_out_from_a_provisional_weight_and_height(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)

    refreshed = _refresh(record.person)

    row, marks = pair(record.person)
    assert float(row.bmi) == pytest.approx(24.2, abs=0.05)
    assert marks.bmi == 75                       # the lowest of its inputs
    # patient_record stays approved-only: not the inputs, not the result.
    assert refreshed.bmi is None and refreshed.weight is None and refreshed.height is None
    assert PatientRecord.objects.get(person=record.person).bmi is None


def test_a_score_with_one_real_and_one_provisional_input_takes_the_provisional_confidence(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=None)        # approved
        _height(record.person, 170, conf=75)         # proposed

    refreshed = _refresh(record.person)

    row, marks = pair(record.person)
    assert float(refreshed.weight) == 70 and refreshed.bmi is None    # real height missing: no real BMI
    assert float(row.bmi) == pytest.approx(24.2, abs=0.05) and marks.bmi == 75
    assert marks.weight is None                                        # the real input carries no mark


def test_the_lowest_of_several_provisional_inputs_wins(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=91)
        _height(record.person, 170, conf=73)

    _refresh(record.person)

    assert pair(record.person)[1].bmi == 73


def test_approving_the_inputs_moves_the_calculated_field_to_the_real_record(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    _refresh(record.person)
    assert pair(record.person)[1].bmi == 75

    Measurement.objects.filter(person=record.person).update(confidence=None)
    refreshed = _refresh(record.person)

    row, marks = pair(record.person)
    assert refreshed.bmi is not None                     # the real refresh calculates it now
    assert marks.bmi is None and marks.weight is None and marks.height is None
    assert row.bmi == refreshed.bmi


def test_a_real_value_beats_a_calculation_from_proposals(snomed, record):
    record.bmi = 31
    marks = {'weight': 82, 'height': 75}

    values, level = provisional_record.computed_proposals(
        record, {'weight': 70, 'height': 170}, marks)
    assert 'bmi' not in values and 'bmi' not in level

    record.bmi = None
    values, level = provisional_record.computed_proposals(
        record, {'weight': 70, 'height': 170}, marks)
    assert level['bmi'] == 75 and values['bmi'] == pytest.approx(24.2, abs=0.05)


def test_an_edited_or_cleared_calculated_field_is_not_refilled(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    PatientRecord.objects.filter(person=record.person).update(
        bmi=None, user_edited_fields=['bmi'])

    _refresh(record.person)

    row, marks = pair(record.person)
    assert row.bmi is None and marks.bmi is None


def test_a_reader_proposed_value_is_not_replaced_by_a_calculation(snomed, record):
    """A measured BMI that is itself a proposal keeps its own value and confidence."""
    values, level = provisional_record.computed_proposals(
        record, {'weight': 70, 'height': 170, 'bmi': 40}, {'weight': 82, 'height': 75, 'bmi': 60})

    assert 'bmi' not in values and 'bmi' not in level


def test_a_proposal_in_the_wrong_unit_is_not_read(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 150, conf=82, unit='lb')     # would be 150 kg to the reader
        _height(record.person, 170, conf=75)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert row.weight is None and row.bmi is None and marks.bmi is None
    assert float(row.height) == 170


def test_a_wrong_unit_newest_weight_does_not_let_an_older_one_stand_in(snomed, record):
    with suppress_patient_record_refresh():
        _vital(record.person, WEIGHT, 60, 'kg', 82, when=date(2023, 1, 1))     # older, readable
        _vital(record.person, WEIGHT, 150, 'lb', 82, when=date(2024, 6, 1))    # newest, not readable
        _height(record.person, 170, conf=75)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert row.weight is None and row.bmi is None and marks.bmi is None       # not last year's 60 kg
    assert float(row.height) == 170


def test_a_proposed_weight_is_dropped_when_the_records_own_unit_differs(snomed, record):
    PatientRecord.objects.filter(person=record.person).update(
        weight_units='lb', user_edited_fields=['weight_units'])
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=75)
        _height(record.person, 170, conf=75)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert row.weight is None and marks.weight is None
    assert row.bmi is None and marks.bmi is None                              # no BMI from 70 read as lb


def test_a_blood_pressure_in_the_wrong_unit_is_not_read(snomed, record):
    with suppress_patient_record_refresh():
        _vital(record.person, '8480-6', 16, 'kPa', 82)

    _refresh(record.person)

    assert not PatientRecordProvisional.objects.filter(systolic_blood_pressure__isnull=False).exists()


def test_a_blood_pressure_in_mmhg_is_read(snomed, record):
    with suppress_patient_record_refresh():
        _vital(record.person, '8480-6', 120, 'mm[Hg]', 82)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert float(row.systolic_blood_pressure) == 120 and marks.systolic_blood_pressure == 82


def test_a_formula_that_owns_the_field_is_left_alone(snomed, record):
    FieldFormula.objects.create(field_name='bmi', formula='weight / (height/100)^2', is_active=True)
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert row.bmi is None and marks.bmi is None


def test_b_symptoms_follows_a_provisional_symptom(snomed, record):
    with suppress_patient_record_refresh():
        _add_observation(record.person, 91)          # fever

    refreshed = _refresh(record.person)

    row, marks = pair(record.person)
    assert row.b_symptoms is True and marks.b_symptoms == 91
    assert refreshed.b_symptoms is None


def test_renal_adequacy_follows_a_provisional_creatinine(snomed, record):
    concept = _concept('Measurement', 3016723, '2160-0', 'Creatinine [Mass/volume] in Serum', 'LOINC')
    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person, measurement_concept=concept, measurement_date=date(2024, 6, 1),
            value_as_number=1.0, measurement_source_value='2160-0', unit_source_value='mg/dL',
            value_source_value='1.0', confidence=88)

    refreshed = _refresh(record.person)

    row, marks = pair(record.person)
    assert row.renal_adequacy_status is True and marks.renal_adequacy_status == 88
    assert refreshed.renal_adequacy_status is None


def test_nothing_is_calculated_with_the_switch_off(snomed, record, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)

    _refresh(record.person)

    assert not PatientRecordProvisional.objects.exists()
    assert not PatientRecordConfidence.objects.exists()


def test_a_failing_calculation_does_not_cost_the_other_proposals(snomed, record, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('calc boom')
    monkeypatch.setattr(provisional_record, 'computed_proposals', boom)
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)

    _refresh(record.person)

    row, marks = pair(record.person)
    assert float(row.weight) == 70 and marks.weight == 82 and marks.bmi is None


def test_a_failing_recalculation_in_the_mirror_does_not_lose_the_edit(snomed, record, staff, monkeypatch):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    _refresh(record.person)

    def boom(*args, **kwargs):
        raise RuntimeError('calc boom')
    monkeypatch.setattr(provisional_record, 'computed_proposals', boom)

    response = staff.patch(f'/api/v1/patient-records/{record.person_id}/',
                           {'weight': '90'}, format='json')

    assert response.status_code == 200, response.content
    row, marks = pair(record.person)
    assert float(row.weight) == 90 and marks.weight is None          # the edit's own mirror still lands


def test_the_record_read_shows_the_calculated_field_marked(snomed, record):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    _refresh(record.person)
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create_user(email='calc-staff@example.test', is_staff=True))

    shown = staff.get(f'/api/v1/patient-records/{record.person_id}/').json()['patient_info']

    assert float(shown['bmi']) == pytest.approx(24.2, abs=0.05)
    assert shown['provisional_fields']['bmi'] == 75
    assert PatientRecord.objects.get(person=record.person).bmi is None


# --- the PATCH mirror ----------------------------------------------------------------

@pytest.fixture()
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='calc-mirror@example.test', is_staff=True))
    return client


def test_an_edit_to_an_input_recalculates_the_proposed_field(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    _refresh(record.person)

    response = staff.patch(f'/api/v1/patient-records/{record.person_id}/',
                           {'weight': '90'}, format='json')

    assert response.status_code == 200, response.content
    row, marks = pair(record.person)
    assert float(row.weight) == 90 and marks.weight is None          # the edit is approved
    assert float(row.bmi) == pytest.approx(31.1, abs=0.05)           # from 90 kg, not the old 70
    assert marks.bmi == 75                                          # the height is still a proposal
    assert PatientRecord.objects.get(person=record.person).bmi is None


def test_clearing_an_input_drops_a_proposal_that_was_calculated_from_it(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=None)        # approved
        _height(record.person, 170, conf=75)
    _refresh(record.person)
    assert pair(record.person)[1].bmi == 75

    staff.patch(f'/api/v1/patient-records/{record.person_id}/', {'weight': None}, format='json')

    row, marks = pair(record.person)
    assert row.weight is None and row.bmi is None and marks.bmi is None
