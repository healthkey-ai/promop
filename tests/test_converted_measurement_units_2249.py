"""A value converted on write is read in the unit it is stored in (#2249).

Only a value the sent number proves is read: one equal to it is in the
source unit, one equal to its conversion is in unit_concept's. Any other row
whose unit_concept names a different, convertible unit is unknown, and its
value is not read as either.
"""
from datetime import date
from decimal import Decimal

import pytest

from omop_core.models import Measurement
from omop_core.services.canonical_units import (
    measurement_stored_unit, normalize_measurement_units, value_in_source_unit,
)
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory
from tests.test_canonical_units import _sccm, _ucum_concept, setup_units  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db


def _row(source_unit, stored_unit, value, sent, **extra):
    return MeasurementFactory(
        unit_source_value=source_unit, value_as_number=Decimal(value), value_source_value=sent,
        unit_concept=_ucum_concept(stored_unit) if stored_unit else None, **extra,
    )


@pytest.mark.parametrize('source_unit,stored,value,sent,expected_value,expected_unit', [
    # The issue's patient A: 3.9 K/uL stored as 3.9e9 /L.
    ('K/uL', '/L', '3900000000', '3.9', Decimal('3.9'), '/L'),
    # Labelled /L, but the number is still as sent.
    ('K/uL', '/L', '3.9', '3.9', Decimal('3.9'), 'K/uL'),
    # Older writes converted the number as stored, rounded to five places.
    ('kg', 'g', '61234.97', '61.23496995', Decimal('61.23496995'), 'g'),
    # No sent number to prove it either way.
    ('g/dL', 'g/L', '40', None, None, None),
    ('g/dL', 'g/L', '40', 'see note', None, None),
    # The sent number proves neither.
    ('mg/dL', 'g/L', '0.00014', '14.4', None, None),
    # One unit spelled two ways, and no unit concept: read as before.
    ('mmHg', 'mm[Hg]', '87', None, Decimal('87'), 'mmHg'),
    ('K/uL', None, '3.9', None, Decimal('3.9'), 'K/uL'),
])
def test_the_sent_number_decides_the_unit(source_unit, stored, value, sent, expected_value, expected_unit):
    row = _row(source_unit, stored, value, sent)

    assert value_in_source_unit(row) == expected_value
    assert measurement_stored_unit(row) == expected_unit


def test_a_converted_count_derives_in_its_source_unit():
    """The issue's patient A: ANC was empty after an overflow warning."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    _row('K/uL', '/L', '3900000000', '3.9', person=person,
         measurement_concept=ConceptFactory(concept_code='751-8'), measurement_source_value='751-8')

    record = refresh_patient_record(person)

    assert record.anc_thousand_per_ul == Decimal('3.9')


def test_an_unproven_conversion_is_not_derived_and_not_rewritten():
    """40 g/L must not be read as 40 g/dL of albumin."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    row = _row('g/dL', 'g/L', '40', None, person=person,
               measurement_concept=ConceptFactory(concept_code='1751-7'), measurement_source_value='1751-7')

    record = refresh_patient_record(person)

    assert record.albumin_g_dl is None
    row.refresh_from_db()
    assert row.value_as_number == Decimal('40')


def test_a_conversion_on_write_keeps_the_number_as_sent():
    concept = ConceptFactory()
    row = MeasurementFactory(measurement_concept=concept, measurement_source_value='ALB',
                             value_as_number=Decimal('4.2'), unit_source_value='g/dL')
    mapping = _sccm('ALB', concept, destination_unit_concept=_ucum_concept('mg/dL'))

    assert normalize_measurement_units(Measurement.objects.filter(pk=row.pk), mapping) == 1

    row = Measurement.objects.select_related('unit_concept').get(pk=row.pk)
    assert (row.value_as_number, row.value_source_value) == (Decimal('4200'), '4.2')
    assert value_in_source_unit(row) == Decimal('4.2')


def test_lab_results_label_a_converted_value_with_its_stored_unit(setup_units):
    client, _, concept, _ = setup_units
    person = PersonFactory()
    PatientRecordFactory(person=person)
    row = _row('g/dL', 'g/L', '42', '4.2', person=person, measurement_concept=concept,
               range_low=Decimal('35'), range_high=Decimal('50'))
    unknown = _row('g/dL', 'g/L', '40', None, person=person, measurement_concept=concept)

    detail = client.get(f'/api/v1/lab-results/measurements/{row.pk}/').json()
    summary = client.get(f'/api/v1/lab-results/summary/?person_id={person.pk}').json()
    units = {v['measurement_id']: v['unit'] for v in summary['results'][0]['values']}

    assert detail['unit'] == 'g/L'
    assert units == {row.pk: 'g/L', unknown.pk: None}


def test_an_older_result_does_not_stand_in_for_an_unproven_newer_one():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code='1751-7')
    MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value='1751-7',
                       measurement_date=date(2025, 1, 1), value_as_number=Decimal('3.5'),
                       unit_source_value='g/dL')
    _row('g/dL', 'g/L', '40', None, person=person, measurement_concept=concept,
         measurement_source_value='1751-7', measurement_date=date(2026, 1, 1))

    assert refresh_patient_record(person).albumin_g_dl is None


def test_a_proven_newer_result_is_used_over_an_unproven_older_one():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code='1751-7')
    _row('g/dL', 'g/L', '40', None, person=person, measurement_concept=concept,
         measurement_source_value='1751-7', measurement_date=date(2025, 1, 1))
    _row('g/dL', 'g/L', '42', '4.2', person=person, measurement_concept=concept,
         measurement_source_value='1751-7', measurement_date=date(2026, 1, 1))

    record = refresh_patient_record(person)

    record.refresh_from_db()
    assert record.albumin_g_dl == Decimal('4.2')


def test_phr_labs_show_a_converted_value_in_its_stored_unit_and_hide_an_unproven_one(settings):
    """The review's case: stored 40, unit_source_value g/dL, unit_concept g/L, nothing sent."""
    from tests.test_phr_read_model import lab, loinc, signed_in
    settings.CELERY_BROKER_URL = ''
    record = PatientRecordFactory(disease='')
    albumin = loinc('1751-7', 'Albumin [Mass/volume] in Serum or Plasma')
    g_l = _ucum_concept('g/L')
    lab(record, albumin, '2024-01-01', Decimal('42'), unit_source_value='g/dL', unit_concept=g_l,
        value_source_value='4.2', range_low=Decimal('35'), range_high=Decimal('50'))
    lab(record, albumin, '2024-05-01', Decimal('40'), unit_source_value='g/dL', unit_concept=g_l,
        range_low=Decimal('35'), range_high=Decimal('50'))
    client = signed_in(record)

    test = client.get('/api/v1/phr/labs/').data['tests'][0]
    history = client.get(f'/api/v1/phr/labs/{albumin.concept_id}/').data['history']

    unproven = {'date', 'value', 'unit', 'range', 'flag'} & set(test['latest'])
    assert unproven == {'date'}
    assert test['recent'] == [{'date': '2024-05-01'}, {'date': '2024-01-01', 'value': 42, 'flag': 'normal'}]
    assert [(h['date'], h.get('value'), h.get('unit'), h.get('flag')) for h in history] == [
        ('2024-05-01', None, None, None),
        ('2024-01-01', 42, 'g/L', 'normal'),
    ]
