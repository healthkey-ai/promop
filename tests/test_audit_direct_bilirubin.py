"""The direct-bilirubin audit reports the historical classes and never writes (#2318)."""
import io
import json
from decimal import Decimal

import pytest
from django.core.management import call_command

from omop_core.models import Measurement, PatientRecord
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db

LOINC = '1968-7'


def _audit(*args):
    out = io.StringIO()
    call_command('audit_direct_bilirubin', *args, '--json', stdout=out)
    return json.loads(out.getvalue())


def _set_pair(record, value, unit, alias):
    # Bypass save/refresh: the audit reads the stored state, including the
    # historical mismatches the normal write path would correct.
    PatientRecord.objects.filter(pk=record.pk).update(
        serum_bilirubin_level_direct=value,
        serum_bilirubin_level_direct_units=unit,
        serum_bilirubin_level=alias,
    )


def test_a_clean_record_is_not_flagged():
    record = PatientRecordFactory()
    _set_pair(record, Decimal('1.20'), 'MG/DL', Decimal('1.20'))

    report = _audit()

    assert report['counts'] == {}
    assert report['scanned'] == 1


def test_it_reports_the_historical_classes():
    missing_unit = PatientRecordFactory()
    _set_pair(missing_unit, Decimal('1.20'), None, Decimal('1.20'))
    unknown_unit = PatientRecordFactory()
    _set_pair(unknown_unit, Decimal('1.20'), 'MG/L', Decimal('1.20'))
    stale_alias = PatientRecordFactory()
    _set_pair(stale_alias, Decimal('10.00'), 'UMOL/L', Decimal('10.00'))
    orphan = PatientRecordFactory()
    _set_pair(orphan, None, None, Decimal('2.00'))

    report = _audit()

    assert report['counts']['pair_without_unit'] == 1
    assert report['counts']['unknown_unit'] == 1
    assert report['counts']['alias_mismatch'] == 1
    assert report['counts']['orphan_alias'] == 1
    assert report['samples']['alias_mismatch'][0]['person_id'] == stale_alias.person_id
    assert 'should be 0.58 mg/dL' in report['samples']['alias_mismatch'][0]['detail']


def test_a_pending_alias_is_reported_without_being_touched():
    record = PatientRecordFactory(
        user_edited_fields=['serum_bilirubin_level'])
    _set_pair(record, Decimal('10.00'), 'UMOL/L', Decimal('0.70'))

    report = _audit()

    assert report['counts']['pending_alias'] == 1
    record.refresh_from_db()
    assert record.serum_bilirubin_level == Decimal('0.70')


def test_it_flags_the_old_raw_unit_projection():
    person = PatientRecordFactory().person
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC),
        measurement_source_value=LOINC,
        value_as_number=10,
        unit_source_value='mg/dL',
    )
    record = PatientRecord.objects.get(person=person)
    _set_pair(record, Decimal('10.00'), 'UMOL/L', Decimal('0.58'))

    report = _audit()

    assert report['counts']['projection_raw_unit'] == 1
    assert report['samples']['projection_raw_unit'][0]['person_id'] == person.person_id


def test_it_flags_a_unitless_or_unknown_projection():
    unitless_person = PatientRecordFactory().person
    MeasurementFactory(
        person=unitless_person,
        measurement_concept=ConceptFactory(concept_code=LOINC),
        measurement_source_value=LOINC,
        value_as_number=Decimal('1.20'),
        unit_source_value=None,
    )
    _set_pair(PatientRecord.objects.get(person=unitless_person),
              Decimal('1.20'), 'MG/DL', Decimal('1.20'))

    unknown_person = PatientRecordFactory().person
    MeasurementFactory(
        person=unknown_person,
        measurement_concept=ConceptFactory(concept_code=LOINC),
        measurement_source_value=LOINC,
        value_as_number=Decimal('1.20'),
        unit_source_value='mg/L',
    )
    _set_pair(PatientRecord.objects.get(person=unknown_person),
              Decimal('1.20'), 'MG/DL', Decimal('1.20'))

    report = _audit()

    assert report['counts']['projection_unitless'] == 1
    assert report['counts']['projection_unknown_unit'] == 1


def test_person_id_scopes_the_audit():
    first = PatientRecordFactory()
    _set_pair(first, Decimal('1.20'), None, Decimal('1.20'))
    second = PatientRecordFactory()
    _set_pair(second, Decimal('1.20'), None, Decimal('1.20'))

    report = _audit('--person-id', str(first.person_id))

    assert report['scanned'] == 1
    assert report['samples']['pair_without_unit'][0]['person_id'] == first.person_id
