"""The direct-bilirubin audit reports the historical classes and never writes (#2318)."""
import io
import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

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
    assert 'does not match' in report['samples']['alias_mismatch'][0]['detail']


def test_a_pending_alias_is_reported_without_being_touched():
    record = PatientRecordFactory(
        user_edited_fields=['serum_bilirubin_level'])
    _set_pair(record, Decimal('10.00'), 'UMOL/L', Decimal('0.70'))

    report = _audit()

    assert report['counts']['pending_alias'] == 1
    record.refresh_from_db()
    assert record.serum_bilirubin_level == Decimal('0.70')
    assert record.user_edited_fields == ['serum_bilirubin_level']


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


def test_a_pending_clear_is_reported_even_when_every_value_is_empty():
    record = PatientRecordFactory(user_edited_fields=['serum_bilirubin_level'])
    _set_pair(record, None, None, None)

    report = _audit()

    assert report['counts']['pending_alias'] == 1
    record.refresh_from_db()
    assert record.serum_bilirubin_level is None
    assert record.serum_bilirubin_level_direct is None
    assert record.user_edited_fields == ['serum_bilirubin_level']


def test_an_older_mg_dl_fact_with_another_number_is_not_flagged():
    person = PatientRecordFactory().person
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC),
        measurement_source_value=LOINC,
        value_as_number=5,
        unit_source_value='mg/dL',
    )
    _set_pair(PatientRecord.objects.get(person=person),
              Decimal('10.00'), 'UMOL/L', Decimal('0.58'))

    report = _audit()

    assert 'projection_raw_unit' not in report['counts']


def test_the_newest_measurement_decides_the_projection_class():
    person = PatientRecordFactory().person
    concept = ConceptFactory(concept_code=LOINC)
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value=LOINC, measurement_date='2024-01-01',
        value_as_number=10, unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value=LOINC, measurement_date='2024-02-01',
        value_as_number=Decimal('0.58'), unit_source_value='mg/dL',
    )
    _set_pair(PatientRecord.objects.get(person=person),
              Decimal('10.00'), 'UMOL/L', Decimal('0.58'))

    report = _audit()

    # The newest fact carries the converted number, not the old raw one.
    assert 'projection_raw_unit' not in report['counts']


def test_a_negative_limit_is_a_command_error():
    with pytest.raises(CommandError):
        call_command('audit_direct_bilirubin', '--limit', '-1')


def test_samples_carry_person_ids_and_no_lab_values():
    """The docstring promises person ids only; samples printed the value,
    unit, alias and the expected number (#2489)."""
    record = PatientRecordFactory()
    _set_pair(record, Decimal('10.00'), 'UMOL/L', Decimal('10.00'))
    unknown = PatientRecordFactory()
    _set_pair(unknown, Decimal('3.21'), 'MG/L', Decimal('3.21'))

    report = _audit()
    text = io.StringIO()
    call_command('audit_direct_bilirubin', stdout=text)

    for rows in report['samples'].values():
        for row in rows:
            assert set(row) == {'person_id', 'detail'}
    output = json.dumps(report) + text.getvalue()
    for value in ('10.00', '0.58', '3.21', 'MG/L', 'UMOL/L'):
        assert value not in output


def test_a_full_run_bounds_measurements_with_a_subquery():
    """A full run inlined every scanned person id into `IN (...)` (#2489)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    records = [PatientRecordFactory() for _ in range(3)]
    for record in records:
        _set_pair(record, Decimal('1.20'), 'MG/DL', Decimal('1.20'))

    with CaptureQueriesContext(connection) as queries:
        _audit()

    measurement_sql = [q['sql'] for q in queries.captured_queries
                       if '"measurement"' in q['sql']]
    assert measurement_sql
    for sql in measurement_sql:
        assert 'IN (SELECT' in sql.upper().replace('IN(SELECT', 'IN (SELECT')
        for record in records:
            assert f'IN ({record.person_id}' not in sql


def test_limit_scans_only_the_first_records():
    for _ in range(3):
        _set_pair(PatientRecordFactory(), Decimal('1.20'), None, Decimal('1.20'))

    report = _audit('--limit', '2')

    assert report['scanned'] == 2
    assert report['counts']['pair_without_unit'] == 2
