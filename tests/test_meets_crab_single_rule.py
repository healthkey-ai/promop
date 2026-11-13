"""meets_crab has one rule: a full refresh and an edit agree (#2480, #2482, #2485).

The refresh used to compute its own CRAB fallback from fixed LOINCs, while an
edit to any CRAB input re-derived it with ``derive_meets_crab`` from the
record's canonical fields. The two disagreed: an approved calcium alias set
``serum_calcium_mg_dl`` but not CRAB until some later, unrelated edit; an old
clearance counted forever; eGFR stood in for clearance on one path only.

The thresholds are the SME's (CancerBot #3593, MM_Disease_Activity slides 5,
12 and 15): CrCl < 40 mL/min, creatinine > 2 mg/dL (read when no clearance is
recorded, #2521), no eGFR, and "not enough data" is Unknown.
"""
from datetime import date

import pytest

from omop_core.services.patient_record_service import (
    derive_meets_crab,
    refresh_patient_record,
)
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db

OLD, NEW = date(2020, 1, 1), date(2026, 10, 1)


def _measure(person, code, value, unit='', day=NEW, **extra):
    return MeasurementFactory(
        person=person, measurement_source_value=code, measurement_date=day,
        value_as_number=value, unit_source_value=unit, **extra)


def _coded(person, loinc, display, value, unit='', day=NEW):
    """An EHR row: the LOINC on the concept, a display name as source value."""
    concept = ConceptFactory(concept_code=loinc, concept_name=display)
    return MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=display,
        measurement_date=day, value_as_number=value, unit_source_value=unit)


# Each scenario builds facts for one person; the expected value is the SME rule.
SCENARIOS = {
    'calcium alias 49765-1 above 11': (
        lambda p: _measure(p, '49765-1', 12.0, 'mg/dL'), True),
    'low hemoglobin': (
        lambda p: _measure(p, '718-7', 9.0, 'g/dL'), True),
    'normal SI hemoglobin': (
        lambda p: _measure(p, '718-7', 8.1, 'mmol/L'), None),
    'SI creatinine above 2 mg/dL': (
        lambda p: _measure(p, '2160-0', 221, 'umol/L'), True),
    'old low clearance, newer normal one': (
        lambda p: (_measure(p, '2164-2', 35, 'mL/min', day=OLD),
                   _measure(p, '2164-2', 90, 'mL/min')), None),
    'coded EHR clearance below 40': (
        lambda p: _coded(p, '2164-2', 'Creatinine clearance', 30, 'mL/min'), True),
    'eGFR below 40 is not a clearance': (
        lambda p: _measure(p, '33914-3', 30, 'mL/min/1.73m2'), None),
    'negative clearance is not evidence': (
        lambda p: _measure(p, '2164-2', -5, 'mL/min'), None),
    'zero clearance is a placeholder': (
        lambda p: _measure(p, '2164-2', 0, 'mL/min'), None),
    'zero clearance defers to a high creatinine': (
        lambda p: (_measure(p, '2164-2', 0, 'mL/min'),
                   _measure(p, '2160-0', 3.0, 'mg/dL')), True),
    'all four assessed and negative': (
        lambda p: (_measure(p, '718-7', 13.0, 'g/dL'),
                   _measure(p, '17861-6', 9.5, 'mg/dL'),
                   _measure(p, '2164-2', 80, 'mL/min'),
                   _measure(p, '24646-7', 0)), False),
    'bone lesions present': (
        lambda p: _measure(p, '24646-7', 1), True),
}


@pytest.mark.parametrize('name', SCENARIOS)
def test_refresh_gives_the_sme_verdict(name):
    build, expected = SCENARIOS[name]
    person = PersonFactory()
    build(person)
    assert refresh_patient_record(person).meets_crab is expected


@pytest.mark.parametrize('name', SCENARIOS)
def test_refresh_and_edit_agree(name):
    """The invariant behind #2480: re-deriving from the stored inputs, as any
    edit to one of them does, never moves the verdict a refresh stored."""
    build, _expected = SCENARIOS[name]
    person = PersonFactory()
    build(person)
    record = refresh_patient_record(person)
    assert record.meets_crab is derive_meets_crab(record)


def test_the_alias_calcium_sets_the_input_and_the_verdict_together():
    """#2480's case: before, the refresh stored the calcium and left CRAB unset."""
    person = PersonFactory()
    _measure(person, '49765-1', 12.0, 'mg/dL')
    record = refresh_patient_record(person)
    assert float(record.serum_calcium_mg_dl) == 12.0
    assert record.meets_crab is True


def test_a_same_day_unitless_molar_row_does_not_hide_a_readable_hemoglobin():
    """#2482: a unitless 59260-0 row with the higher id, on the same day as a
    readable 8.0 g/dL 718-7 result, no longer hides the anaemia."""
    person = PersonFactory()
    readable = _measure(person, '718-7', 8.0, 'g/dL')
    molar = _measure(person, '59260-0', 8.0, '')
    assert molar.measurement_id > readable.measurement_id
    assert refresh_patient_record(person).meets_crab is True


def test_a_stated_crab_fact_wins_over_the_inputs():
    """An explicit 89599-5 result is the source's own verdict and stands."""
    person = PersonFactory()
    _measure(person, '718-7', 8.0, 'g/dL')
    _measure(person, '89599-5', 0)
    assert refresh_patient_record(person).meets_crab is False


@pytest.mark.parametrize('answer', ['Unknown', 'unknown', 'Not assessed'])
def test_an_unknown_bone_lesion_answer_is_not_a_lesion(answer):
    """'Unknown' is a curated bone_lesions choice. Read as a lesion it set CRAB
    with no evidence; it is unknown, so the verdict stays unknown."""
    from omop_core.models import PatientRecord
    person = PersonFactory()
    PatientRecord.objects.update_or_create(
        person=person, defaults={'bone_lesions': answer, 'user_edited_fields': ['bone_lesions']})

    record = refresh_patient_record(person)

    assert record.bone_lesions == answer
    assert record.meets_crab is None


def test_an_unknown_bone_lesion_answer_leaves_a_negative_work_up_unknown():
    from omop_core.models import PatientRecord
    person = PersonFactory()
    _measure(person, '718-7', 13.0, 'g/dL')
    _measure(person, '17861-6', 9.5, 'mg/dL')
    _measure(person, '2164-2', 80, 'mL/min')
    PatientRecord.objects.update_or_create(
        person=person, defaults={'bone_lesions': 'Unknown', 'user_edited_fields': ['bone_lesions']})

    assert refresh_patient_record(person).meets_crab is None
